import {jobs,stations} from '../api/entities.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {StationStatus,DirectiveStatus,DirectiveKind} from '../constants/enums.js';
import {esc,date,fmt,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';

const canAdmin=()=>session.user?.role==='admin';

function versionBadge(version){
 if(version===null||version===undefined)return '<span class="version none">未生效</span>';
 return '<span class="version">v'+version+'</span>';
}

function snapshotLine(snapshot){
 if(!snapshot||!Object.keys(snapshot).length)return '<p class="hint">尚无冻结快照。</p>';
 return '<div class="snapshot"><span>型号 <strong>'+esc(snapshot.model||'—')+'</strong></span><span>目标可靠度 <strong>'+esc(fmt(snapshot.reliability))+'</strong></span><span>目标里程 <strong>'+esc(fmt(snapshot.mileage))+'</strong> 公里</span><span>来源任务 <strong>#'+esc(snapshot.job_id??'—')+'</strong></span></div>';
}

function directiveRow(d){
 const kind=DirectiveKind[d.kind]||d.kind;
 const label=DirectiveStatus[d.status]||d.status;
 const lost=d.receipt_lost?'<span class="flag lost">回执丢失</span>':'';
 const actions=d.pending&&canAdmin()
   ?'<button class="text-button" data-retry="'+d.id+'" data-station="'+d.station_id+'">重试 ↻</button><button class="text-button" data-receipt="'+d.id+'" data-station="'+d.station_id+'">查回回执</button>'
   :'';
 return '<tr><td>#'+d.id+'</td><td>'+esc(kind)+' '+versionBadge(d.version)+'</td><td><span class="status '+esc(d.status)+'">'+esc(label)+'</span>'+lost+'</td><td>'+d.attempts+'</td><td>'+esc(d.last_error||'—')+'</td><td>'+actions+'</td></tr>';
}

function stationCard(row){
 const s=row.station, eff=row.station_effective, drill=row.drill||{};
 const confirmed=s.confirmed_version, stationVersion=eff.effective_version;
 const pending=row.pending||[];
 const pendingLine=pending.length
   ?'<div class="pending-line"><strong>待核对指令</strong>'+pending.map(d=>'<span>'+esc(DirectiveKind[d.kind]||d.kind)+' v'+d.version+' · '+esc(DirectiveStatus[d.status]||d.status)+(d.receipt_lost?' · 回执丢失，先查回原结果':'')+'</span>'+(canAdmin()?'<span class="pending-actions"><button class="text-button" data-retry="'+d.id+'" data-station="'+s.id+'">重试 ↻</button><button class="text-button" data-receipt="'+d.id+'" data-station="'+s.id+'">查回回执</button></span>':'')).join('')+'</div>'
   :'<p class="hint ok">两侧均已核对，无待决指令。</p>';
 return '<article class="station-card status-'+esc(row.status)+'"><header><div><strong>'+esc(s.name)+'</strong><span>'+esc(s.code)+'</span></div><span class="status '+esc(row.status)+'">'+esc(StationStatus[row.status]||row.status)+'</span></header><div class="two-sided"><div><span class="eyebrow">业务侧确认版本</span>'+versionBadge(confirmed)+snapshotLine(s.confirmed_snapshot)+'</div><div><span class="eyebrow">站端生效版本</span>'+versionBadge(stationVersion)+snapshotLine(eff.effective_payload)+'</div></div>'+pendingLine+'<div class="station-drill"><div class="drill-switches"><span class="eyebrow">站端演练台</span><label class="switch"><input type="checkbox" data-drill="online" data-station="'+s.id+'" '+(drill.online?'checked':'')+' '+(canAdmin()?'':'disabled')+'> 在线</label><label class="switch"><input type="checkbox" data-drill="drop_receipt" data-station="'+s.id+'" '+(drill.drop_receipt?'checked':'')+' '+(canAdmin()?'':'disabled')+'> 模拟漏回执</label><label class="switch"><input type="checkbox" data-drill="hold" data-station="'+s.id+'" '+(drill.hold?'checked':'')+' '+(canAdmin()?'':'disabled')+'> 暂扣指令（乱序）</label><span class="hint">暂扣 '+drill.held+' 条</span><button class="button small" data-release="'+s.id+'" data-order="lifo" '+(canAdmin()&&drill.held?'':'disabled')+'>按新到先发释放</button><button class="button small" data-release="'+s.id+'" data-order="fifo" '+(canAdmin()&&drill.held?'':'disabled')+'>按先到先发释放</button></div><div class="station-actions"><button class="button small" data-reconcile="'+s.id+'" '+(canAdmin()?'':'disabled')+'>对账此站</button><button class="button small" data-revoke="'+s.id+'" data-version="'+(confirmed??'')+'" '+(canAdmin()&&confirmed?'':'disabled')+'>撤销 v'+esc(confirmed??'—')+'</button></div></div><details class="directive-log"><summary>指令记录（最近 20 条）</summary><div class="table-wrap"><table><thead><tr><th>指令</th><th>版本</th><th>状态</th><th>尝试</th><th>最近说明</th><th>操作</th></tr></thead><tbody>'+(row.directives||[]).map(directiveRow).join('')+'</tbody></table></div></details></article>';
}

export async function stationPage(){
 const [board,allJobs]=await Promise.all([stations.board(),jobs.list()]);
 const approved=allJobs.filter(j=>j.approved&&j.status==='completed');
 const rows=board.stations||[];
 const inSync=rows.filter(r=>r.status==='in_sync').length;
 const unresolved=rows.reduce((n,r)=>n+(r.pending||[]).length,0);
 return heading('06 / STATION CONTROL','各站检修指令','批准估计按站点冻结型号、里程与版本后下发；逐站核对回执，漏回执先查回原结果再重试。','<button class="button" id="refresh-stations">刷新站况 ↻</button>')+'<section class="station-summary"><div><span>已接入站点</span><strong>'+rows.length+'</strong></div><div><span>两侧一致</span><strong>'+inSync+'</strong></div><div><span>待核对指令</span><strong>'+unresolved+'</strong></div><div><span>最近对账</span><strong>'+(rows[0]?.station.last_reconciled_at?date(rows[0].station.last_reconciled_at):'—')+'</strong></div></section><div class="station-grid"><section class="panel"><div class="section-title"><h2>按站下发已批准估计</h2><span>'+approved.length+' 条可下发结果</span></div><form id="publish-form"><label>已批准分析结果<select name="job_id" required>'+approved.map(j=>'<option value="'+j.id+'">#'+j.id+' · '+esc(j.name)+' · '+esc(j.params?.model||'')+'</option>').join('')+'</select></label><label>接收站点<select name="station_id" required>'+rows.filter(r=>r.station.enabled).map(r=>'<option value="'+r.station.id+'">'+esc(r.station.name)+' · '+esc(r.station.code)+'</option>').join('')+'</select></label><button type="submit" class="primary" '+(canAdmin()&&approved.length&&rows.length?'':'disabled')+'>冻结并下发 ↗</button><p class="hint">下发时冻结型号、目标里程、目标可靠度与版本号；同一批结果未核对前不会重复下发。仅业务负责人可操作。</p></form><div class="section-title"><h2>新增站点</h2></div><form id="station-form"><label>站点编码<input name="code" required minlength="2" pattern="[A-Za-z0-9_-]+" placeholder="例如 DEPOT-D"></label><label>站点名称<input name="name" required minlength="2" placeholder="例如 鹭湾四号检修站"></label><button type="submit" '+(canAdmin()?'':'disabled')+'>接入站点</button></form><div class="section-title"><h2>全局对账</h2></div><button class="button" id="reconcile-all" '+(canAdmin()?'':'disabled')+'>查询各站并补齐丢失回执</button><p class="hint">对账只读取站端台账与当前生效版本，不会重复生效任何指令。</p></section><section><div class="section-title"><h2>站点核对板</h2><span>业务侧与站端两侧版本</span></div><div class="station-list">'+(rows.length?rows.map(stationCard).join(''):'<p class="empty">尚无站点。先在左侧接入一个检修站。</p>')+'</div></section></div>';
}

let refreshPage=()=>{};

async function act(fn,success){
 try{await fn();notify(success);await refreshPage();}catch(error){notify(error.message,true);}
}

export function bindStations(refresh){
 refreshPage=refresh;
 document.querySelector('#refresh-stations')?.addEventListener('click',()=>refreshPage());
 document.querySelector('#reconcile-all')?.addEventListener('click',()=>act(()=>stations.reconcileAll(),'已完成全局对账'));
 submit(document.querySelector('#publish-form'),async f=>{
   const d=await stations.publish(Number(f.get('station_id')),Number(f.get('job_id')));
   notify(d.status==='effective'?'已下发并获站端回执':'已下发，等待站端回执核对');
   await refreshPage();
 });
 submit(document.querySelector('#station-form'),async f=>{
   await stations.create({code:f.get('code'),name:f.get('name')});notify('站点已接入');await refreshPage();
 });
 document.querySelectorAll('[data-reconcile]').forEach(b=>b.onclick=()=>act(()=>stations.reconcile(b.dataset.reconcile),'已对账该站'));
 document.querySelectorAll('[data-revoke]').forEach(b=>b.onclick=()=>{
   const version=Number(b.dataset.version)||null;
   if(!confirm('确认撤销该站版本 v'+(version??'当前')+'？撤销在站端确认前会保持待核对。'))return;
   act(()=>stations.revoke(b.dataset.revoke,version),'撤销指令已下发，等待站端确认');
 });
 document.querySelectorAll('[data-release]').forEach(b=>b.onclick=()=>act(()=>stations.release(b.dataset.release,b.dataset.order),'已释放暂扣指令'));
 document.querySelectorAll('[data-retry]').forEach(b=>b.onclick=()=>act(()=>stations.retry(b.dataset.station,b.dataset.retry),'已重试，重试不会重复生效'));
 document.querySelectorAll('[data-receipt]').forEach(b=>b.onclick=async()=>{
   try{const r=await stations.receipt(b.dataset.station,b.dataset.receipt);const text=r.receipt?('站端台账：'+r.receipt.outcome+' · 当前生效 '+versionText(r.state.effective_version)):'站端台账无此指令回执';notify(text);}catch(e){notify(e.message,true);}
 });
 document.querySelectorAll('[data-drill]').forEach(input=>input.onchange=()=>act(
   ()=>stations.drill(input.dataset.station,{[input.dataset.drill]:input.checked}),
   '站端演练开关已更新'));
}

function versionText(v){return v===null||v===undefined?'无':'v'+v;}

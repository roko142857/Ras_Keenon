const $=id=>document.getElementById(id);
const canvas=$('mapCanvas'), ctx=canvas.getContext('2d');
let state=null,image=null,imageVersion='',view={scale:1,x:0,y:0},initialized=false;
let mode='browse',draft=[],selected=null,route=[],down=null,moving=false,sessionId=localStorage.getItem('keenon-navigation-session'),chatBusy=false,lastChat='';
let pendingImage=0,toastTimer=null,fetchBusy=false;
const labels={exploring:'模拟探索中',navigating:'模拟路线执行中',paused:'已暂停',completed:'模拟任务结束',partial:'部分完成',cancelled:'已停止',failed:'任务失败',interrupted:'已中断'};
function toast(text){$('toast').textContent=text;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,5000);}
async function api(path,data){
  const response=await fetch('/api/navigation/'+path,{method:data===undefined?'GET':'POST',headers:{'Content-Type':'application/json'},body:data===undefined?undefined:JSON.stringify(data),cache:'no-store',signal:AbortSignal.timeout(10000)});
  const result=await response.json();if(!response.ok)throw Error(result.error||'请求失败');return result;
}
async function chatApi(path,data){
  const response=await fetch('/api/'+path,{method:data===undefined?'GET':'POST',headers:{'Content-Type':'application/json'},body:data===undefined?undefined:JSON.stringify(data),cache:'no-store',signal:AbortSignal.timeout(10000)});
  const result=await response.json();if(!response.ok){const error=Error(result.error||'对话请求失败');error.status=response.status;throw error;}return result;
}
function action(id,fn){$(id).onclick=async()=>{const button=$(id);button.disabled=true;try{await fn();await refresh();}catch(e){toast(e.message);}finally{button.disabled=false;}};}
function mapPixel(p){const [ox,oy,yaw]=state.map.origin;const x=p[0]-ox,y=p[1]-oy,c=Math.cos(yaw),s=Math.sin(yaw);return [(c*x+s*y)/state.map.resolution,state.map.height-(-s*x+c*y)/state.map.resolution];}
function worldAt(x,y){const u=(x-view.x)/view.scale,v=state.map.height-(y-view.y)/view.scale;const r=state.map.resolution,[ox,oy,a]=state.map.origin;return [ox+r*(Math.cos(a)*u-Math.sin(a)*v),oy+r*(Math.sin(a)*u+Math.cos(a)*v)];}
function screen(p){const [x,y]=mapPixel(p);return [view.x+x*view.scale,view.y+y*view.scale];}
function resize(){const r=canvas.getBoundingClientRect(),dpr=window.devicePixelRatio||1;canvas.width=Math.round(r.width*dpr);canvas.height=Math.round(r.height*dpr);ctx.setTransform(dpr,0,0,dpr,0,0);if(initialized)draw();}
function fit(){if(!state?.map)return;const r=canvas.getBoundingClientRect();view.scale=Math.min((r.width-45)/state.map.width,(r.height-45)/state.map.height);view.x=(r.width-state.map.width*view.scale)/2;view.y=(r.height-state.map.height*view.scale)/2;initialized=true;draw();}
function line(points,color,width=2,close=false,fill=null,dash=[]){
  if(!points?.length)return;ctx.beginPath();points.forEach((p,i)=>{const q=screen(p);if(i)ctx.lineTo(...q);else ctx.moveTo(...q);});if(close)ctx.closePath();ctx.setLineDash(dash);if(fill){ctx.fillStyle=fill;ctx.fill();}ctx.strokeStyle=color;ctx.lineWidth=width;ctx.stroke();ctx.setLineDash([]);
}
function dot(p,color,r=4,label=''){const q=screen(p);ctx.fillStyle=color;ctx.beginPath();ctx.arc(...q,r,0,2*Math.PI);ctx.fill();if(label){ctx.font='11px sans-serif';ctx.fillStyle='#284e52';ctx.fillText(label,q[0]+7,q[1]-7);}}
function draw(){
  const box=canvas.getBoundingClientRect();ctx.clearRect(0,0,box.width,box.height);if(!state?.map)return;
  if(image){ctx.imageSmoothingEnabled=false;ctx.drawImage(image,view.x,view.y,state.map.width*view.scale,state.map.height*view.scale);}
  if(state.boundary)line(state.boundary,'#12857c',2,true,'#12857c0c',[5,4]);
  state.exclusions.forEach(p=>line(p,'#b74e60',2,true,'#b74e6026',[5,3]));
  line(state.trail,'#7ab7a9',2);line(state.path,'#25879e',2.5);
  state.candidates.forEach(p=>dot([p.x,p.y],'#d99a2e',4));
  const current=state.waypoints.filter(p=>p.map_id===state.map.map_id);
  current.forEach(p=>dot([p.x,p.y],'#5c8c91',4,p.name));
  const points=route.map(s=>current.find(p=>p.id===s.waypoint_id)).filter(Boolean);
  line(points.map(p=>[p.x,p.y]),'#8d82ae',1.5,false,null,[4,4]);
  points.forEach((p,i)=>dot([p.x,p.y],'#8d82ae',4,String(i+1)));
  if(state.goal)dot([state.goal.x,state.goal.y],'#df8660',6,'目标');
  if(state.pose){const [x,y]=screen(state.pose);const yaw=state.pose[2]-state.map.origin[2];
  ctx.save();ctx.translate(x,y);ctx.rotate(-yaw);ctx.fillStyle='#137d76';ctx.beginPath();ctx.moveTo(12,0);ctx.lineTo(-8,-7);ctx.lineTo(-5,0);ctx.lineTo(-8,7);ctx.closePath();ctx.fill();ctx.restore();}
  if(draft.length){line(draft,mode==='exclusion'?'#b74e60':'#0c8a7b',2,false,null,[4,3]);draft.forEach((p,i)=>dot(p,'#167e78',4,String(i+1)));}
  if(selected){dot([selected.x,selected.y],'#8c64ab',6,'选点');const end=[selected.x+Math.cos(selected.yaw)*.45,selected.y+Math.sin(selected.yaw)*.45];line([[selected.x,selected.y],end],'#8c64ab',2);}
}
canvas.addEventListener('pointerdown',event=>{if(!state?.map)return;canvas.setPointerCapture(event.pointerId);const r=canvas.getBoundingClientRect();down={x:event.clientX-r.left,y:event.clientY-r.top,vx:view.x,vy:view.y};moving=false;if(mode==='waypoint'){const p=worldAt(down.x,down.y);selected={x:p[0],y:p[1],yaw:Number($('waypointYaw').value)*Math.PI/180};draw();}});
canvas.addEventListener('pointermove',event=>{if(!state?.map)return;const r=canvas.getBoundingClientRect(),x=event.clientX-r.left,y=event.clientY-r.top,p=worldAt(x,y);$('coordinates').textContent=`map · ${p[0].toFixed(2)}, ${p[1].toFixed(2)} m`;if(!down)return;
  if(Math.hypot(x-down.x,y-down.y)>4)moving=true;
  if(mode==='browse'){view.x=down.vx+x-down.x;view.y=down.vy+y-down.y;}
  if(mode==='waypoint'&&moving&&selected){selected.yaw=Math.atan2(p[1]-selected.y,p[0]-selected.x);$('waypointYaw').value=Math.round(selected.yaw*180/Math.PI);}
  draw();
});
canvas.addEventListener('pointerup',event=>{if(!down||!state)return;const r=canvas.getBoundingClientRect(),p=worldAt(event.clientX-r.left,event.clientY-r.top);if((mode==='region'||mode==='exclusion')&&!moving)draft.push(p);if(selected)$('selectedPoint').textContent=`选点 ${selected.x.toFixed(2)}, ${selected.y.toFixed(2)} m · ${Math.round(selected.yaw*180/Math.PI)}°`;down=null;draw();});
canvas.addEventListener('pointercancel',()=>{down=null;});
canvas.addEventListener('wheel',event=>{event.preventDefault();if(!state?.map)return;const r=canvas.getBoundingClientRect(),x=event.clientX-r.left,y=event.clientY-r.top,factor=event.deltaY<0?1.12:1/1.12,old=view.scale;view.scale=Math.max(.5,Math.min(60,old*factor));view.x=x-(x-view.x)*view.scale/old;view.y=y-(y-view.y)*view.scale/old;draw();},{passive:false});
$('mapMode').onchange=()=>{mode=$('mapMode').value;draft=[];const hints={browse:'滚轮缩放，拖动地图。',waypoint:'点击选择位置，按住拖拽设置到达朝向。',region:'逐个点击多边形顶点，然后点击“应用绘制”。区域须包含机器人及机身净空。',exclusion:'逐个点击禁入区顶点，然后点击“应用绘制”。'};$('mapHint').textContent=hints[mode];draw();};
$('waypointYaw').oninput=()=>{if(selected){selected.yaw=Number($('waypointYaw').value)*Math.PI/180;draw();}};
action('fitMap',fit);action('preview',async()=>{const v=await api('preview',{});toast(`找到 ${v.candidates.length} 个模拟探索候选`);});
action('startExplore',async()=>{await api('start',{mode:'explore',request_id:crypto.randomUUID(),duration_s:Number($('duration').value)});route=[];toast('模拟探索任务已提交');});
for(const [id,a] of [['pauseTask','pause'],['resumeTask','resume'],['stopTask','stop'],['finishTask','finish']])action(id,async()=>{await api('control',{action:a});if(a==='finish')await exportsList();});
action('manualPreempt',async()=>{const v=await api('manual-preempt',{});toast(v.task?.state==='paused'?'本地手动接管事件：探索任务已暂停':'当前没有运行中的模拟任务');});
action('undoVertex',()=>{draft.pop();draw();});
action('clearRegion',async()=>{await api('region',{boundary:null,exclusions:[]});draft=[];toast('已清除区域');});
action('applyRegion',async()=>{if(draft.length<3)throw Error('请先绘制至少三个顶点');if(!['region','exclusion'].includes(mode))throw Error('请先选择区域绘制模式');await api('region',{boundary:mode==='region'?draft:state.boundary,exclusions:mode==='exclusion'?[...state.exclusions,draft]:state.exclusions});draft=[];toast('区域已应用');});
action('saveWaypoint',async()=>{if(!selected)throw Error('请在地图中选择命名点');await api('waypoints',{...selected,yaw:Number($('waypointYaw').value)*Math.PI/180,name:$('waypointName').value});$('waypointName').value='';selected=null;$('selectedPoint').textContent='尚未选点';});
action('saveRoute',async()=>{await api('routes',{name:$('routeName').value,steps:route});toast('路线已保存');});
action('runRoute',async()=>{await api('start',{mode:'route',steps:route,request_id:crypto.randomUUID()});toast('模拟路线已提交');});
action('saveMap',async()=>{await api('save',{});await exportsList();toast('模拟地图快照已导出');});
$('savedRoutes').onchange=()=>{const name=$('savedRoutes').value;if(name){route=structuredClone(state.routes[name]);renderRoute();draw();}};
function renderRoute(){const list=$('routeSteps');list.replaceChildren();route.forEach((step,index)=>{const w=state?.waypoints.find(p=>p.id===step.waypoint_id),li=document.createElement('li'),row=document.createElement('div'),span=document.createElement('span'),input=document.createElement('input');li.className='route-step';span.textContent=w?.name||'旧地图命名点';input.type='number';input.min=0;input.max=300;input.value=step.dwell_s||0;input.setAttribute('aria-label',`路径点 ${index+1} 停留秒数`);input.onchange=()=>{step.dwell_s=Number(input.value);};row.append(span,input,document.createTextNode('秒'));
  for(const [label,fn] of [['↑',()=>{if(index){[route[index-1],route[index]]=[route[index],route[index-1]];}}],['↓',()=>{if(index<route.length-1){[route[index+1],route[index]]=[route[index],route[index+1]];}}],['×',()=>route.splice(index,1)]]){const b=document.createElement('button');b.textContent=label;b.setAttribute('aria-label',`路径点 ${index+1} ${label}`);b.onclick=()=>{fn();renderRoute();draw();};row.append(b);}li.append(row);list.append(li);});$('routeCount').textContent=route.length+' 个路径点';}
let waypointsSignature='',lastExport='';
function renderWaypoints(){const current=state.waypoints.filter(p=>p.map_id===state.map.map_id),signature=JSON.stringify(current);if(signature!==waypointsSignature){waypointsSignature=signature;const area=$('waypoints');area.replaceChildren();current.forEach(p=>{const chip=document.createElement('span'),label=document.createElement('span'),add=document.createElement('button'),remove=document.createElement('button');chip.className='waypoint-chip';label.textContent=p.name;add.textContent='＋';add.setAttribute('aria-label','加入路线 '+p.name);add.onclick=()=>{route.push({waypoint_id:p.id,dwell_s:0});renderRoute();draw();};remove.textContent='×';remove.setAttribute('aria-label','删除命名点 '+p.name);remove.onclick=async()=>{try{await api('waypoints/remove',{id:p.id});await refresh();}catch(e){toast(e.message);}};chip.append(label,add,remove);area.append(chip);});if(!current.length){const p=document.createElement('p');p.className='hint';p.textContent='选取可达位置后，可保存命名点并加入路线。';area.append(p);}}
 const select=$('savedRoutes'),keys=Object.keys(state.routes);if(select.dataset.keys!==JSON.stringify(keys)){select.dataset.keys=JSON.stringify(keys);const value=select.value;select.replaceChildren(new Option('选择已保存路线',''),...keys.map(k=>new Option(k,k)));select.value=keys.includes(value)?value:'';}}
async function exportsList(){try{const list=await api('exports');$('exports').replaceChildren();list.slice(0,8).forEach(item=>{const p=document.createElement('div');p.className='export';const label=document.createElement('div');label.textContent=item.id;p.append(label);for(const filename of ['map.pgm','map.yaml','exploration_report.json']){const a=document.createElement('a');a.href='/api/navigation/exports/'+item.id+'/'+filename;a.textContent=filename==='exploration_report.json'?'报告':filename;a.download=filename;p.append(a);}const b=document.createElement('button');b.textContent='加载模拟快照';b.onclick=async()=>{try{await api('load',{id:item.id});route=[];selected=null;await refresh();renderRoute();toast('已加载模拟栅格，不是 SLAM 续建');}catch(e){toast(e.message);}};if(!state?.read_only)p.append(b);$('exports').append(p);});}catch(e){toast(e.message);}}
function renderBackend(){
  const live=state.read_only===true;
  $('backendBadge').textContent=live?'原厂只读':'本地模拟';
  canvas.setAttribute('aria-label',live?'原厂已发布地图，仅支持查看、缩放和导出':'模拟探索地图，支持拖动、缩放、选点和绘制区域');
  if($('navigationChatHint'))$('navigationChatHint').textContent=live?'可查询原厂状态或导出当前栅格。当前未开放建图和导航控制。':'可输入开始探索、暂停探索或保存地图。API 模式使用同一套导航工具。';
  if($('navigationChatEmpty'))$('navigationChatEmpty').textContent=live?'对话查询真实地图与只读状态；不会执行运动。':'让模型安排高层任务，地图展示实际模拟进度。';
  $('chatInput').placeholder=live?'查询导航状态 / 保存地图':'开始探索当前区域';
  const factoryMode=state.factory_mode?`${state.robot_connection?'原厂':'最后收到'}：${state.factory_mode.label}（${state.factory_mode.code}）`:'原厂模式等待反馈';
  $('backendNotice').textContent=live?`真实底盘地图 · ${factoryMode} · 定位语义未确认 · 建图与运动未开通。`:'合成双房间环境 · 模拟 360° 扫描 · 不连接机器人。栅格导出可回放，不能作为真实 SLAM 地图。';
  document.querySelector('.battery').textContent=live?'电量：可在手动控制页查询':'电量 — · 本地不读取机器人';
  $('settingsHint').textContent=live?'对话可查询真实地图。模型设置不改变只读权限；API Key 仅保存在后端内存。':'导航服务始终为本地模拟。API Key 仅保存在对话后端内存。';
  for(const id of ['startExplore','pauseTask','resumeTask','finishTask','stopTask','manualPreempt','applyRegion','undoVertex','clearRegion','preview','saveWaypoint','saveRoute','runRoute','mapMode'])$(id).disabled=live;
  $('startExplore').textContent=live?'自主探索尚待实机验证':'开始模拟探索';
  $('saveMap').textContent=live?'导出原厂地图快照':'保存模拟地图';
  $('saveMap').disabled=!state.map;
  if(live)$('mapHint').textContent='可平移缩放和导出当前栅格；未将未确认位姿绘制为正常定位。';
  if(live){mode='browse';draft=[];selected=null;$('taskState').textContent='原厂只读';$('taskReason').textContent=state.error||state.blocked_reason;$('connection').textContent=state.robot_connection?'原厂只读订阅已连接':'等待原厂只读连接';}
}
async function refresh(){if(fetchBusy)return;fetchBusy=true;try{const next=await api('state'),oldMap=state?.map?.map_id;state=next;renderBackend();
  if(!state.map){$('mapMeta').textContent='等待原厂地图';$('knownArea').textContent='—';draw();return;}
  $('connection').textContent=state.read_only?(state.robot_connection?'原厂只读订阅已连接':'原厂连接中断，保留上次地图'):'服务已连接 · 机器人未连接';$('taskState').textContent=state.task?(state.task_historical?'上次任务 · ':'')+(labels[state.task.state]||state.task.state):'待开始';$('taskReason').textContent=state.task?.reason||(state.task?.state==='paused'?'任务已暂停，模拟目标已取消。':state.task?.mode==='route'?'按命名点顺序执行模拟路线。':'选择范围并开始模拟任务；目标由探索器自动选择。');if(state.read_only){$('taskState').textContent='原厂只读';$('taskReason').textContent=state.blocked_reason;}
  $('knownArea').textContent=state.map.known_area_m2.toFixed(1);$('targetCount').textContent=state.task?.targets_completed||0;$('elapsed').textContent=Math.floor(state.task?.elapsed_s||0);$('mapMeta').textContent=`${state.read_only?'原厂地图':'模拟双房间'} · ${state.map.width} × ${state.map.height} · ${Number(state.map.resolution.toFixed(3))} m / 格`;
  if(oldMap&&oldMap!==state.map.map_id){route=[];selected=null;renderRoute();}
  if(state.map.version!==imageVersion){imageVersion=state.map.version;const ticket=++pendingImage,img=new Image();img.onload=()=>{if(ticket!==pendingImage)return;image=img;if(!initialized)fit();else draw();};img.src='/api/navigation/map.png?v='+encodeURIComponent(imageVersion);}
  if(state.task?.saved_map&&state.task.saved_map!==lastExport){lastExport=state.task.saved_map;exportsList();}
  renderWaypoints();draw();$('events').replaceChildren(...state.events.slice(-12).reverse().map(e=>{const p=document.createElement('div');p.textContent=new Date(e.time*1000).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai'})+' · '+e.title;return p;}));
 }catch(e){$('connection').textContent='本地导航服务未连接';}finally{fetchBusy=false;}}
async function pollChat(){if(!sessionId)return;try{const data=await chatApi('sessions/'+sessionId),signature=JSON.stringify([data.messages,data.receipts]);if(signature===lastChat)return;lastChat=signature;$('chatMessages').replaceChildren(...data.messages.filter(m=>m.content).map(m=>{const d=document.createElement('div');d.className='chat-message '+m.role;const label=document.createElement('small');label.textContent=m.role==='user'?'你':'助手';d.append(label,document.createTextNode(m.content));return d;}));$('chatMessages').scrollTop=$('chatMessages').scrollHeight;$('receiptCount').textContent=data.receipts.length;
  $('chatReceipts').replaceChildren(...data.receipts.slice(-16).reverse().map(r=>{const d=document.createElement('div');d.className='receipt '+r.state;const name=document.createElement('div');name.textContent=r.title+' · '+r.state;const info=document.createElement('small');info.textContent=r.kind==='llm'?'模型消息回执':'接口回执（不等于任务完成）';const details=document.createElement('details'),summary=document.createElement('summary'),pre=document.createElement('pre');summary.textContent='查看内容';pre.textContent=JSON.stringify(r.data,null,2);details.append(summary,pre);d.append(name,info,details);return d;}));
 }catch(e){if(e.status===404){sessionId=null;localStorage.removeItem('keenon-navigation-session');}toast(e.message);}}
$('chatForm').onsubmit=async event=>{event.preventDefault();if(chatBusy)return;const text=$('chatInput').value.trim();if(!text)return;chatBusy=true;$('sendChat').disabled=true;try{if(!sessionId){sessionId=(await chatApi('sessions',{})).id;localStorage.setItem('keenon-navigation-session',sessionId);}await chatApi('chat',{session_id:sessionId,message:text});$('chatInput').value='';await pollChat();}catch(e){toast(e.message);}finally{chatBusy=false;$('sendChat').disabled=false;}};
$('chatInput').onkeydown=event=>{if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();$('chatForm').requestSubmit();}};
async function config(){const cfg=await chatApi('config');$('llmMode').textContent=cfg.mode==='demo'?'演示规则':'兼容 API';return cfg;}
$('modelSettings').onclick=async()=>{try{const cfg=await config();$('settingMode').value=cfg.mode;$('settingBase').value=cfg.base_url;$('settingModel').value=cfg.model;$('settingKey').value='';$('settingKey').placeholder=cfg.key_configured?'已配置，留空保留':'填写 API Key';$('settingsDialog').showModal();}catch(e){toast(e.message);}};
$('closeSettings').onclick=()=>$('settingsDialog').close();
$('settingsForm').onsubmit=async event=>{event.preventDefault();try{await chatApi('config',{mode:$('settingMode').value,base_url:$('settingBase').value,model:$('settingModel').value,api_key:$('settingKey').value});$('settingKey').value='';$('settingsDialog').close();await config();toast('模型设置已更新');}catch(e){toast(e.message);}};
new ResizeObserver(resize).observe($('mapWrap'));await refresh();await exportsList();config().catch(e=>toast(e.message));pollChat();
setInterval(()=>{if(!document.hidden){refresh();pollChat();}},800);
document.addEventListener('visibilitychange',()=>{if(!document.hidden){refresh();pollChat();}});

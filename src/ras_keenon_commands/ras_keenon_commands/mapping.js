export function mountMappingPanel({clearKeys=()=>{},adoptSession=()=>{}}={}){
  const $=id=>document.getElementById(id),canvas=$('mappingCanvas'),ctx=canvas.getContext('2d');
  if(!$('mapLibrary'))$('mappingExisting').insertAdjacentHTML('beforebegin',"<div id=\"mapLibrary\" class=\"map-library\">\n <div class=\"mapping-heading\"><h3>已有地图管理</h3><span id=\"mapLibraryCurrent\" class=\"mapping-muted\"></span></div>\n <p class=\"mapping-muted\">选择一张地图后预览、备份或编辑；预览不会切换当前地图。</p>\n <label class=\"mapping-muted\" for=\"mapLibrarySelect\">地图列表</label><select id=\"mapLibrarySelect\" aria-label=\"选择已有地图\"><option value=\"\">请选择已有地图</option></select>\n <div id=\"mapLibraryDetail\" class=\"mapping-muted\">尚未选择地图</div>\n <div class=\"map-library-fields\"><label>地图名称<input id=\"mapLibraryName\" maxlength=\"80\"></label><label>楼栋<input id=\"mapLibraryBuilding\" maxlength=\"80\"></label><label>楼层说明<input id=\"mapLibraryFloorInfo\" maxlength=\"80\"></label></div>\n <div class=\"mapping-actions\"><button id=\"mapLibraryPreview\" type=\"button\">预览</button><button id=\"mapLibraryExport\" type=\"button\">导出备份</button><button id=\"mapLibrarySwitch\" type=\"button\">切换到此图</button><button id=\"mapLibraryUpdate\" type=\"button\">保存信息</button><button id=\"mapLibraryReplace\" type=\"button\">覆盖重建</button></div>\n <p class=\"mapping-muted\">覆盖重建在收到本次新图后才更新旧图，旧图自动备份；原导航点和标签需复核。Cartographer 地图仅预览、备份、切换及编辑信息。</p>\n <div id=\"mapLibraryPreviewBox\" hidden><p id=\"mapLibraryPreviewTitle\"></p><img id=\"mapLibraryImage\" alt=\"所选已有地图预览\"><div id=\"mapLibraryDownloads\" class=\"mapping-downloads\"></div></div>\n</div>\n");
  if(!$('mappingNew'))$('mappingStart').insertAdjacentHTML('afterend','<button id="mappingNew" type="button" hidden>取消覆盖，改为新建</button>');
  for(const p of document.querySelectorAll('#mappingPanel p'))if(p.textContent.includes('首版保存为新楼层'))p.textContent='松键 / 空格停止驾驶，SLAM继续采集。支持新楼层保存或明确选择已有Gmapping地图覆盖；快照不包含可续建SLAM状态。';
  if(!$('mapConfirm'))$('mapLibrary').insertAdjacentHTML('afterend','<dialog id="mapConfirm" aria-labelledby="mapConfirmTitle"><h3 id="mapConfirmTitle">确认地图操作</h3><p id="mapConfirmText"></p><div class="mapping-actions"><button id="mapConfirmYes" type="button">确认执行</button><button id="mapConfirmNo" type="button">取消</button></div></dialog>');
  function confirmAction(text){clearKeys();return new Promise(resolve=>{const dialog=$('mapConfirm');$('mapConfirmText').textContent=text;let done=false;const finish=value=>{if(done)return;done=true;dialog.close();resolve(value);};$('mapConfirmYes').onclick=()=>finish(true);$('mapConfirmNo').onclick=()=>finish(false);dialog.oncancel=e=>{e.preventDefault();finish(false);};dialog.showModal();});}
  const phases={queued:'请求已受理',starting:'等待原厂启动回执',waiting_image:'等待本次 SLAM 图像',mapping:'手动建图中',saving:'保存地图中',returning:'返回导航模式',saved:'原厂地图已保存',cancelling:'退出建图中',cancelled:'本次建图已取消',error:'操作未完成',result_unknown:'结果待确认',recovery_required:'恢复后的会话待确认'};
  const actions={start:'开始建图',finish:'结束并保存',cancel:'取消建图',export:'导出快照',refresh:'刷新列表',map_preview:'预览已有地图',map_export:'导出已有地图',map_switch:'切换地图',map_update:'编辑地图信息'},states={queued:'已受理',running:'处理中',succeeded:'成功',failed:'失败',uncertain:'结果待确认'};
  let state=null,polling=false,submitting=false,image=null,imageKey='',ticket=0,scale=1,dx=0,dy=0,drag=null,receiptsKey='',formSession=null;
  let libraryKey='',libraryRows=[],replaceTarget=null,previewReceipt='';
  const targetOf=r=>({floor:r.floor,type:r.type,md5:r.md5});
  const selected=()=>libraryRows.find(r=>libraryId(r)===$('mapLibrarySelect').value);
  const libraryId=r=>r.floor+':'+r.type+':'+r.md5;
  const uid=()=>crypto.randomUUID?crypto.randomUUID():Date.now().toString(36)+Math.random().toString(36).slice(2);
  function size(){const r=canvas.getBoundingClientRect(),d=window.devicePixelRatio||1;if(canvas.width!==Math.round(r.width*d)||canvas.height!==Math.round(r.height*d)){canvas.width=Math.round(r.width*d);canvas.height=Math.round(r.height*d);fit();}}
  function fit(){if(image){scale=Math.min(canvas.width/image.width,canvas.height/image.height)*.92;dx=(canvas.width-image.width*scale)/2;dy=(canvas.height-image.height*scale)/2;}draw();}
  function draw(){ctx.clearRect(0,0,canvas.width,canvas.height);if(image){ctx.imageSmoothingEnabled=false;ctx.drawImage(image,dx,dy,image.width*scale,image.height*scale);}else{ctx.fillStyle='#6b8387';ctx.font=`${13*(window.devicePixelRatio||1)}px system-ui`;ctx.textAlign='center';ctx.fillText(state?.session?'等待本次建图画面':'等待原厂地图',canvas.width/2,canvas.height/2);}}
  function zoom(f,x=canvas.width/2,y=canvas.height/2){if(!image)return;const next=Math.max(.01,Math.min(100,scale*f));dx=x-(x-dx)*next/scale;dy=y-(y-dy)*next/scale;scale=next;draw();}
  canvas.addEventListener('wheel',e=>{e.preventDefault();const r=canvas.getBoundingClientRect(),d=window.devicePixelRatio||1;zoom(Math.exp(-e.deltaY*.001),(e.clientX-r.left)*d,(e.clientY-r.top)*d);},{passive:false});
  canvas.addEventListener('pointerdown',e=>{canvas.setPointerCapture(e.pointerId);drag=[e.clientX,e.clientY,dx,dy];});
  canvas.addEventListener('pointermove',e=>{if(!drag)return;const d=window.devicePixelRatio||1;dx=drag[2]+(e.clientX-drag[0])*d;dy=drag[3]+(e.clientY-drag[1])*d;draw();});
  for(const event of ['pointerup','pointercancel','lostpointercapture'])canvas.addEventListener(event,()=>{drag=null;});
  $('mapFit').onclick=fit;$('mapZoomIn').onclick=()=>zoom(1.3);$('mapZoomOut').onclick=()=>zoom(1/1.3);new ResizeObserver(size).observe(canvas);
  function render(){
    const s=state.session,active=!!s&&!['saved','cancelled'].includes(s.phase),busy=!!state.busy||submitting;
    if(s&&formSession!==s.id){replaceTarget=null;for(const [id,key] of [['mappingName','name'],['mappingBuilding','buildingInfo'],['mappingFloor','floor'],['mappingFloorInfo','floorInfo']])$(id).value=s.form[key];formSession=s.id;}
    $('mappingBackend').textContent=state.simulated?'本地模拟':'原厂 Gmapping';
    const mode=state.factory.mode;
    $('mappingSummary').textContent=(state.simulated?'模拟建图，不连接机器人':'真实原厂接口 · 人工遥控建图')+' · '+(state.factory.connected?'已连接':'连接中断')+' · 原厂模式 '+(mode===null?'未知':mode===0?'导航（0）':mode);
    $('mappingPhase').textContent=s?(phases[s.phase]||s.phase):'等待创建新地图';
    $('mappingNote').textContent=s?.note||state.factory.error||'创建 Gmapping 新地图后，使用遥控按键驾驶机器人覆盖环境。';
    $('mappingUpdated').textContent=state.image?'图像接收 '+new Date(state.image.wall*1000).toLocaleTimeString():'等待图源';
    $('mappingSource').textContent=state.image?`${state.image.width} × ${state.image.height} · ${state.image.topic}`:'等待地图图源';
    $('mappingStart').textContent=replaceTarget?'覆盖重建 · 楼层 '+replaceTarget.floor:state.simulated?'开始模拟 Gmapping 建图':'开始 Gmapping 建图';
    $('mappingNew').hidden=!replaceTarget;$('mappingNew').disabled=busy||active;
    $('mappingStart').disabled=busy||active||!state.factory.connected||mode!==0;
    for(const id of ['mappingName','mappingBuilding','mappingFloor','mappingFloorInfo'])$(id).disabled=active||busy||(id==='mappingFloor'&&!!replaceTarget);
    $('mappingFinish').disabled=busy||!active||(!state.image&&!s?.prepared);
    $('mappingFinish').textContent=s?.stale_save_confirmation_required&&!s?.prepared?'仍保存停更快照':'结束并保存';
    if(active&&!s?.prepared&&state.image?.age_s>30){$('mappingPhase').textContent='建图图像停更';$('mappingNote').textContent=`已 ${Math.round(state.image.age_s)} 秒没有新建图图像；连接在线不代表 SLAM 正常更新。`;} 
    $('mappingCancel').disabled=busy||!(active||(s?.phase==='cancelled'&&s?.original_floor));$('mappingCancel').textContent=!active&&s?.phase==='cancelled'?'确认返回导航模式':'取消本次建图';$('mappingExport').disabled=busy||!s||(!s.prepared&&(!active||!state.image));$('mappingRefresh').disabled=busy;
    renderLibrary(active,busy);
    $('mappingExisting').textContent=state.maps_error?'地图列表查询：'+state.maps_error:'已有楼层：'+(state.maps.map(x=>`${x.buildingInfo} / ${x.floor}（${x.type}）`).join('、')||'无记录');
    if(!active&&!$('mappingFloor').value&&state.maps_updated_at){let next=1;while(state.maps.some(x=>x.floor===next)||next%200===0)next++;$('mappingFloor').value=next;}
    const key=(state.image?state.image.version:'')+':'+(s?.id||'');
    if(!state.image){if(image||imageKey){image=null;imageKey='';ticket++;draw();}}
    else if(key!==imageKey){imageKey=key;const current=++ticket,img=new Image();img.onload=()=>{if(current!==ticket)return;const first=!image;image=img;first?fit():draw();};img.onerror=()=>{if(current===ticket){imageKey='';$('mappingMessage').textContent='地图图像暂未加载，正在重试。';}};img.src='/mapping/map.png?v='+encodeURIComponent(key);}
    const receiptHash=JSON.stringify(state.operations);
    if(receiptHash!==receiptsKey){receiptsKey=receiptHash;const area=$('mappingReceipts');area.replaceChildren();for(const op of [...state.operations].reverse().slice(0,12)){
      const row=document.createElement('div'),title=document.createElement('strong'),time=document.createElement('small'),note=document.createElement('div'),detail=document.createElement('details'),summary=document.createElement('summary'),pre=document.createElement('pre');row.className='mapping-receipt';row.dataset.state=op.state;title.textContent=(actions[op.action]||op.action)+' · '+(states[op.state]||op.state);time.textContent=new Date(op.time*1000).toLocaleString()+' · '+op.id;note.textContent=op.note;summary.textContent='查看回执';pre.textContent=JSON.stringify(op.result||{state:op.state,note:op.note},null,2);detail.append(summary,pre);row.append(title,time,note,detail);area.append(row);
      for(const archive of [op.result,op.result?.backup])if(archive?.export_id){for(const file of archive.files||[]){const link=document.createElement('a');link.href='/mapping/exports/'+archive.export_id+'/'+file;link.textContent=(archive===op.result?'':'旧图备份 · ')+file;link.download=file;link.style.marginRight='12px';row.append(link);}}
    }if(!state.operations.length)area.textContent='尚无建图操作；打开页面不会开始 SLAM。';}
  }
  async function refresh(){if(polling)return;polling=true;try{const r=await fetch('/mapping/state'),v=await r.json();if(!r.ok)throw Error(v.error||r.status);state=v;render();}catch(e){$('mappingSummary').textContent='建图后台不可用 · 手动控制可继续使用';$('mappingNote').textContent=e.message;for(const id of ['mappingStart','mappingFinish','mappingCancel','mappingExport'])$(id).disabled=true;}finally{polling=false;}}
  async function submit(action,value={}){if(submitting)return;submitting=true;$('mappingMessage').textContent='正在提交…';clearKeys();try{const body={...value,request_id:uid()};const managed=action.startsWith('map_');if(managed)body.management_action=action;if(action!=='start'&&state?.session)body.session_id=state.session.id;const r=await fetch('/mapping/'+(managed?'refresh':action),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),v=await r.json();if(!r.ok)throw Error(v.error||r.status);if(v.controller_session)adoptSession(v.controller_session);$('mappingMessage').textContent='请求已受理，实际执行结果见回执。';}catch(e){$('mappingMessage').textContent=e.message;}finally{submitting=false;await refresh();}}
  $('mappingForm').onsubmit=async e=>{e.preventDefault();const payload={name:$('mappingName').value,floor:Number($('mappingFloor').value),floorInfo:$('mappingFloorInfo').value||$('mappingFloor').value,buildingInfo:$('mappingBuilding').value,...(replaceTarget?{replace_target:{...replaceTarget}}:{})};if(payload.replace_target&&!await confirmAction(`将重新建图并在保存时覆盖楼层 ${payload.floor} 的所选地图。旧图将备份，原导航点/标签需复核。确定开始？`))return;submit('start',payload);};
  $('mappingFinish').onclick=()=>submit('finish',state?.session?.stale_save_confirmation_required?{allow_stale:true}:{});$('mappingCancel').onclick=()=>submit('cancel');$('mappingExport').onclick=()=>submit('export');$('mappingRefresh').onclick=()=>submit('refresh');

  function renderLibrary(active,busy){
    libraryRows=state.maps||[];
    const select=$('mapLibrarySelect'),old=select.value,key=JSON.stringify([libraryRows,state.current_floor]);
    if(key!==libraryKey){libraryKey=key;select.replaceChildren(new Option('请选择已有地图',''));for(const row of libraryRows)select.add(new Option(`${row.floor===state.current_floor?'当前 · ':''}${row.buildingInfo} / ${row.floor} · ${row.name} [${row.type}]`,libraryId(row)));select.value=libraryRows.some(r=>libraryId(r)===old)?old:'';if(select.value!==old)fillLibrary();}
    $('mapLibraryCurrent').textContent='当前楼层 '+(state.current_floor??'未知');
    const row=selected();
    $('mapLibraryDetail').textContent=row?`楼层 ${row.floor} · ${row.type} · MD5 ${row.md5}`:'尚未选择地图';
    for(const id of ['mapLibraryPreview','mapLibraryExport'])$(id).disabled=busy||!row||!state.factory.connected;
    for(const id of ['mapLibrarySwitch','mapLibraryUpdate','mapLibraryReplace'])$(id).disabled=busy||active||!row||!state.factory.connected||(id==='mapLibraryReplace'&&row.type!=='map');
    for(const id of ['mapLibraryName','mapLibraryBuilding','mapLibraryFloorInfo'])$(id).disabled=busy||active||!row;
    select.disabled=busy;
    const op=[...(state.operations||[])].reverse().find(o=>['map_preview','map_export'].includes(o.action)&&o.state==='succeeded'&&o.result?.export_id);
    if(op&&previewReceipt!==op.id){previewReceipt=op.id;const r=op.result;$('mapLibraryPreviewBox').hidden=false;$('mapLibraryPreviewTitle').textContent='楼层 '+op.payload.target.floor+' 地图快照 · '+new Date(op.time*1000).toLocaleTimeString();$('mapLibraryImage').src='/mapping/exports/'+r.export_id+'/map.png';const links=$('mapLibraryDownloads');links.replaceChildren();for(const file of r.files){const a=document.createElement('a');a.href='/mapping/exports/'+r.export_id+'/'+file;a.download=file;a.textContent=file;links.append(a);}}
  }
  function fillLibrary(){$('mapLibraryPreviewBox').hidden=true;const row=selected();for(const [id,key] of [['mapLibraryName','name'],['mapLibraryBuilding','buildingInfo'],['mapLibraryFloorInfo','floorInfo']])$(id).value=row?.[key]||'';}
  $('mappingNew').onclick=()=>{replaceTarget=null;let next=1;while(state.maps.some(x=>x.floor===next)||next%200===0)next++;$('mappingFloor').value=next;render();};
  $('mapLibrarySelect').onchange=()=>{fillLibrary();if(state)render();};
  for(const [id,action] of [['mapLibraryPreview','map_preview'],['mapLibraryExport','map_export'],['mapLibrarySwitch','map_switch']])$(id).onclick=async()=>{const row=selected();if(!row)return;if(action==='map_switch'&&!await confirmAction(`切换当前楼层至 ${row.buildingInfo}/${row.floor} ${row.name}？此操作不发送导航目标。`))return;submit(action,{target:targetOf(row)});};
  $('mapLibraryUpdate').onclick=()=>{const row=selected();if(row)submit('map_update',{target:targetOf(row),metadata:{name:$('mapLibraryName').value,buildingInfo:$('mapLibraryBuilding').value,floorInfo:$('mapLibraryFloorInfo').value}});};
  $('mapLibraryReplace').onclick=()=>{const row=selected();if(!row||row.type!=='map')return;replaceTarget=targetOf(row);for(const [id,key] of [['mappingName','name'],['mappingBuilding','buildingInfo'],['mappingFloor','floor'],['mappingFloorInfo','floorInfo']])$(id).value=row[key];render();$('mappingForm').scrollIntoView({behavior:'smooth',block:'center'});$('mappingMessage').textContent='已选择覆盖目标；点击上方“覆盖重建”启动，结束保存时才写入。';};

  setInterval(refresh,500);refresh();size();
}

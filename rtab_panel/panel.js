// RTAB display only. Keyboard and motion handlers belong to the original panel.
export function mountRtabPanel({clearKeys}) {
  const $=id=>document.getElementById(id), canvas=$('rtabCanvas');
  let drawError='', renderer;
  try {renderer=new CloudView(canvas);} catch(e) {drawError=e.message;}
  const names={idle:'未启动',starting:'启动中',running:'采集中',stopping:'保存中',saved:'已保存',error:'采集异常'};
  let busy=false, version='', connected=false, actionError='';
  async function request(path,data) {
    const response=await fetch('/rtab/'+path,data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    const value=await response.json(); if(!response.ok)throw Error(value.error||response.status);return value;
  }
  async function action(name) {
    if(busy)return;busy=true;actionError='';clearKeys();$('rtabStart').disabled=true;$('rtabStop').disabled=true;
    const duration=$('rtabDuration').value;
    try {await request(name,name==='start'?(duration==='until-stop'?{until_stop:true}:{seconds:Number(duration)}):{});} catch(e) {actionError=e.message;}
    finally {busy=false;await poll();}
  }
  $('rtabStart').onclick=()=>action('start');$('rtabStop').onclick=()=>action('stop');
  $('rtabFit').onclick=()=>renderer?.fit();
  async function poll() {
    try {
      const s=await request('state');connected=true;
      const active=['starting','running','stopping'].includes(s.phase);
      $('rtabPhase').textContent=names[s.phase]||s.phase;$('rtabPhase').dataset.error=String(s.phase==='error');
      $('rtabStart').disabled=busy||active;$('rtabStop').disabled=busy||!['starting','running'].includes(s.phase);
      $('rtabDuration').disabled=busy||active;
      $('rtabPoints').textContent=s.points.toLocaleString()+' 点';$('rtabFrames').textContent=`${s.tracked_frames} 跟踪帧 · ${s.lost_frames} 丢失`;
      $('rtabTracking').textContent=s.phase==='running'?(s.tracking?'跟踪正常':'等待有效跟踪'):s.phase==='saved'?'采集已结束':'—';
      $('rtabCountdown').textContent=active?(s.until_stop?'持续采集，直到停止':`剩余约 ${s.remaining_s} 秒`):'';
      $('rtabBasis').textContent=s.basis==='optimized'?'优化后点云 · 米制坐标':'实时视觉里程计预览 · 米制坐标';
      $('rtabPath').textContent=s.run||'尚无采集';
      $('rtabMessage').textContent=[drawError,actionError,...s.errors].filter(Boolean).join('；')||(s.phase==='saved'?'已保存原始数据与 RTAB 结果。':s.phase==='running'?'正在采集；松键停止驾驶，建图继续。':'');
      const key=`${s.run_id}:${s.version}`;
      if(renderer&&key!==version){const map=await request('cloud');renderer.set(map);version=key;$('rtabEmpty').hidden=map.points.length>0;}
    } catch(e) {connected=false;$('rtabPhase').textContent='服务未连接';$('rtabMessage').textContent=e.message;$('rtabStart').disabled=true;$('rtabStop').disabled=true;}
  }
  async function loop(){await poll();setTimeout(loop,connected?1000:2500);}loop();
}

class CloudView {
  constructor(canvas){
    this.canvas=canvas;this.gl=canvas.getContext('webgl',{antialias:true});if(!this.gl)throw Error('浏览器不支持 WebGL，无法显示三维点云');
    const gl=this.gl;
    const vertex=`attribute vec3 position;attribute vec3 color;uniform vec3 center;uniform vec3 right;uniform vec3 up;uniform vec3 eye;uniform float distance;uniform float aspect;uniform float size;varying vec3 c;void main(){vec3 p=position-center;float z=distance-dot(p,eye);gl_Position=vec4(dot(p,right)*1.8/aspect,dot(p,up)*1.8,1.0002*z-.020002,z);gl_PointSize=size;c=color;}`;
    const fragment=`precision mediump float;varying vec3 c;void main(){gl_FragColor=vec4(c,1.);}`;
    const compile=(kind,text)=>{const sh=gl.createShader(kind);gl.shaderSource(sh,text);gl.compileShader(sh);if(!gl.getShaderParameter(sh,gl.COMPILE_STATUS))throw Error(gl.getShaderInfoLog(sh));return sh;};
    this.program=gl.createProgram();gl.attachShader(this.program,compile(gl.VERTEX_SHADER,vertex));gl.attachShader(this.program,compile(gl.FRAGMENT_SHADER,fragment));gl.linkProgram(this.program);if(!gl.getProgramParameter(this.program,gl.LINK_STATUS))throw Error('WebGL程序连接失败');
    this.buffer=gl.createBuffer();this.points=[];this.path=[];this.center=[0,0,0];this.distance=5;this.az=-2.2;this.el=.45;this.initial=true;this.run=null;
    let pointer=null;
    canvas.addEventListener('pointerdown',e=>{pointer=[e.clientX,e.clientY,e.pointerId];canvas.setPointerCapture(e.pointerId);});
    canvas.addEventListener('pointermove',e=>{if(!pointer||pointer[2]!==e.pointerId)return;this.az-=(e.clientX-pointer[0])*.008;this.el=Math.max(-1.4,Math.min(1.4,this.el+(e.clientY-pointer[1])*.008));pointer=[e.clientX,e.clientY,e.pointerId];this.draw();});
    for(const event of ['pointerup','pointercancel','lostpointercapture'])canvas.addEventListener(event,()=>{pointer=null;});
    canvas.addEventListener('wheel',e=>{e.preventDefault();this.distance=Math.max(.15,Math.min(80,this.distance*Math.exp(e.deltaY*.001)));this.draw();},{passive:false});
    new ResizeObserver(()=>this.draw()).observe(canvas);
  }
  set(map){this.points=map.points;this.path=map.trajectory;if(this.run!==map.run_id){this.initial=true;this.run=map.run_id;}if(this.initial&&this.points.length){this.fit();this.initial=false;}else this.draw();}
  fit(){if(this.points.length){const low=[Infinity,Infinity,Infinity],high=[-Infinity,-Infinity,-Infinity];for(const p of this.points)for(let i=0;i<3;i++){low[i]=Math.min(low[i],p[i]);high[i]=Math.max(high[i],p[i]);}this.center=low.map((v,i)=>(v+high[i])/2);this.distance=Math.max(1,Math.hypot(...high.map((v,i)=>v-low[i]))*.95);}this.az=-2.2;this.el=.45;this.draw();}
  draw(){
    const gl=this.gl, c=this.canvas,dpr=Math.min(devicePixelRatio,2);c.width=Math.max(1,Math.round(c.clientWidth*dpr));c.height=Math.max(1,Math.round(c.clientHeight*dpr));
    gl.viewport(0,0,c.width,c.height);gl.clearColor(.035,.105,.13,1);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);gl.enable(gl.DEPTH_TEST);gl.useProgram(this.program);
    const eye=[Math.cos(this.el)*Math.cos(this.az),Math.cos(this.el)*Math.sin(this.az),Math.sin(this.el)],right=[-Math.sin(this.az),Math.cos(this.az),0],up=[-Math.sin(this.el)*Math.cos(this.az),-Math.sin(this.el)*Math.sin(this.az),Math.cos(this.el)];
    const u=(name)=>gl.getUniformLocation(this.program,name);gl.uniform3fv(u('center'),this.center);gl.uniform3fv(u('eye'),eye);gl.uniform3fv(u('right'),right);gl.uniform3fv(u('up'),up);gl.uniform1f(u('distance'),this.distance);gl.uniform1f(u('aspect'),c.width/c.height);
    const batch=(rows,mode,size)=>{if(!rows.length)return;const data=new Float32Array(rows.flat());gl.bindBuffer(gl.ARRAY_BUFFER,this.buffer);gl.bufferData(gl.ARRAY_BUFFER,data,gl.DYNAMIC_DRAW);for(const [name,offset] of [['position',0],['color',12]]){const loc=gl.getAttribLocation(this.program,name);gl.enableVertexAttribArray(loc);gl.vertexAttribPointer(loc,3,gl.FLOAT,false,24,offset);}gl.uniform1f(u('size'),size*dpr);gl.drawArrays(mode,0,rows.length);};
    batch(this.points.map(p=>[p[0],p[1],p[2],p[3]/255,p[4]/255,p[5]/255]),gl.POINTS,2.3);
    // Axes originate at the RTAB origin, not an asserted floor plane.
    batch([[0,0,0,1,.35,.3],[.4,0,0,1,.35,.3],[0,0,0,.3,1,.5],[0,.4,0,.3,1,.5],[0,0,0,.4,.6,1],[0,0,.4,.4,.6,1]],gl.LINES,1);
    gl.disable(gl.DEPTH_TEST);batch(this.path.map(p=>[...p,1,.62,.18]),gl.LINE_STRIP,2);if(this.path.length)batch([[...this.path.at(-1),1,.8,.35]],gl.POINTS,7);
  }
}

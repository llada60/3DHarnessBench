const links=window.PROJECT_LINKS||{};
[['paperLink','paper'],['codeLink','code'],['dataLink','data']].forEach(([id,key])=>{const el=document.getElementById(id);if(el&&links[key]){el.href=links[key];el.classList.remove('disabled');el.removeAttribute('aria-disabled');el.querySelector('span')?.remove();el.target='_blank';el.rel='noopener'}});

const galleryImage=document.getElementById('galleryImage');
const galleryCaption=document.getElementById('galleryCaption');
document.querySelectorAll('.tab').forEach(btn=>btn.addEventListener('click',()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  btn.classList.add('active');
  if(galleryImage&&btn.dataset.img)galleryImage.src=btn.dataset.img;
  if(galleryCaption&&btn.dataset.caption)galleryCaption.textContent=btn.dataset.caption;
}));

const mainRows=[
['Single-view','Fable 5',.913,.731,.784,.012,.733,.340,.815],['Single-view','Opus 5',.910,.704,.764,.015,.725,.340,.830],['Single-view','GPT-5.6 Sol',.915,.714,.783,.016,.715,.332,1.000],['Single-view','Kimi K3',.896,.664,.734,.020,.678,.332,.893],['Single-view','Qwen 3.8 Max',.913,.731,.777,.017,.740,.346,.835],['Single-view','Gemini 3.1 Pro',.904,.692,.756,.016,.728,.335,.776],['Single-view','MiniMax M3',.833,.490,.576,.028,.495,.263,.921],
['Multi-view','Fable 5',.927,.760,.811,.011,.764,.345,.805],['Multi-view','Opus 5',.921,.738,.796,.011,.748,.343,.931],['Multi-view','GPT-5.6 Sol',.925,.764,.816,.015,.745,.326,1.000],['Multi-view','Kimi K3',.909,.702,.765,.014,.713,.338,.925],['Multi-view','Qwen 3.8 Max',.920,.753,.805,.011,.768,.344,.768],['Multi-view','Gemini 3.1 Pro',.908,.700,.774,.015,.753,.331,.736],['Multi-view','MiniMax M3',.847,.522,.617,.022,.577,.299,.983],
['Active Visual','Fable 5',.932,.767,.822,.006,.832,.349,.663],['Active Visual','Opus 5',.939,.796,.843,.004,.871,.347,.611],['Active Visual','GPT-5.6 Sol',.931,.760,.817,.007,.823,.342,.864],['Active Visual','Kimi K3',.925,.740,.802,.005,.819,.336,.685],['Active Visual','Qwen 3.8 Max',.919,.729,.789,.006,.809,.331,.711],['Active Visual','Gemini 3.1 Pro',.865,.600,.674,.015,.691,.315,.853],['Active Visual','MiniMax M3',.808,.447,.527,.034,.482,.250,.974],
['Full 3D Interaction','Fable 5',.949,.818,.859,.003,.884,.345,.557],['Full 3D Interaction','Opus 5',.957,.842,.883,.002,.927,.347,.473],['Full 3D Interaction','GPT-5.6 Sol',.939,.788,.841,.003,.879,.342,.677],['Full 3D Interaction','Kimi K3',.929,.747,.809,.004,.848,.339,.749],['Full 3D Interaction','Qwen 3.8 Max',.934,.763,.822,.004,.857,.339,.750],['Full 3D Interaction','Gemini 3.1 Pro',.901,.694,.759,.008,.783,.322,.588],['Full 3D Interaction','MiniMax M3',.857,.578,.651,.014,.679,.302,.821]];
const mainHeaders=['Harness','Agent','SIGLIP-2 ↑','DINOv2 ↑','DINOv3 ↑','Chamfer ↓','UNI3D ↑','UNI3D I-3D ↑','Betti L1 ↓'];

const effRows=[
['Single-view','Fable 5',.105,.021,.255,11,'—',4.14,51846,2.00,382],['Single-view','Opus 5',.140,.074,.250,12,'—',4.32,101440,2.04,780],['Single-view','GPT-5.6 Sol',.333,.097,.431,4,'—',4,30856,1.26,664],['Single-view','Kimi K3',.233,.077,.318,15,'—',4.39,28199,'—',928],['Single-view','Qwen 3.8 Max',.098,.042,.262,4,'—',4.41,39959,'—',758],['Single-view','Gemini 3.1 Pro',.125,.060,.248,4,'—',4.57,'—','—',429],['Single-view','MiniMax M3',.198,.225,.360,6,'—',6.65,16640,'—',217],
['Multi-view','Fable 5',.108,.061,.241,33,'—',4.11,68625,2.54,428],['Multi-view','Opus 5',.167,.060,.338,34,'—',4.19,130053,2.55,946],['Multi-view','GPT-5.6 Sol',.335,.073,.387,4,'—',4,34308,1.47,744],['Multi-view','Kimi K3',.203,.066,.282,18,'—',4.70,30410,'—',1065],['Multi-view','Qwen 3.8 Max',.102,.040,.204,4,'—',4.24,41173,'—',935],['Multi-view','Gemini 3.1 Pro',.128,.070,.231,4,'—',4.56,'—','—',587],['Multi-view','MiniMax M3',.228,.158,.361,6,'—',6.81,18652,'—',356],
['Active Visual','Fable 5',.146,.021,.098,58,51,7.21,153361,7.93,860],['Active Visual','Opus 5',.095,.018,.155,116,87,17.20,328147,10.74,1790],['Active Visual','GPT-5.6 Sol',.158,.080,.272,55,69,15.09,45004,3.24,939],['Active Visual','Kimi K3',.089,.009,.163,114,41,12.61,53444,'—',2370],['Active Visual','Qwen 3.8 Max',.125,.041,.167,393,55,11.63,341310,'—',5400],['Active Visual','Gemini 3.1 Pro',.070,.024,.209,43,37,10.59,'—','—',469],['Active Visual','MiniMax M3',.167,.149,.333,431,156,23.87,115309,'—',6287],
['Full 3D Interaction','Fable 5',.043,.005,.085,42,18,7.44,154278,6.52,1064],['Full 3D Interaction','Opus 5',.015,.015,.058,80,34,15.18,380011,7.92,1962],['Full 3D Interaction','GPT-5.6 Sol',.090,.006,.139,70,35,12.31,50484,4.54,1344],['Full 3D Interaction','Kimi K3',.040,.017,.176,75,25,11.72,54086,'—',2460],['Full 3D Interaction','Qwen 3.8 Max',.058,.031,.198,257,51,8.88,456831,'—',8206],['Full 3D Interaction','Gemini 3.1 Pro',.036,.006,.083,38,22,6.45,'—','—',448],['Full 3D Interaction','MiniMax M3',.113,.058,.250,172,67,10.59,76278,'—',1905]];
const effHeaders=['Harness','Agent','β0 ↓','β1 ↓','β2 ↓','API Calls','MCP Calls','Versions','Output Tokens','Cost','Latency (s)'];

function makeTable(id,headers,rows){const table=document.getElementById(id);if(!table)return;const head=table.querySelector('thead'),body=table.querySelector('tbody');head.innerHTML='<tr>'+headers.map(h=>`<th>${h}</th>`).join('')+'</tr>';body.innerHTML=rows.map(r=>'<tr>'+r.map((v,i)=>`<td>${typeof v==='number'?(i>1&&!Number.isInteger(v)?v.toFixed(3):v.toLocaleString()):v}</td>`).join('')+'</tr>').join('')}

const copyBib=document.getElementById('copyBib');
const bibtexText=document.getElementById('bibtexText');
if(copyBib&&!bibtexText){
  copyBib.remove();
}else if(copyBib&&bibtexText){
  copyBib.addEventListener('click',async()=>{
    try{
      await navigator.clipboard.writeText(bibtexText.innerText);
      copyBib.textContent='Copied';
      setTimeout(()=>copyBib.textContent='Copy',1200);
    }catch(e){}
  });
}

function initUni3DHarnessChart(){
  const root=document.getElementById('uni3dHarnessChart');
  const svg=document.getElementById('uni3dSvg');
  const legend=document.getElementById('uni3dSettingLegend');
  const agentFilter=document.getElementById('uni3dAgentFilter');
  const tooltip=document.getElementById('uni3dTooltip');
  const resetBtn=document.getElementById('uni3dReset');
  if(!root||!svg||!legend||!agentFilter||!tooltip)return;

  const NS='http://www.w3.org/2000/svg';
  const settings=[
    {name:'Multi-view',color:'#1f77b4',shape:'circle'},
    {name:'Active Visual',color:'#ff7f0e',shape:'square'},
    {name:'Full 3D Interaction',color:'#2ca02c',shape:'triangle'}
  ];
  const agents=['Fable 5','Opus 5','GPT-5.6 Sol','Kimi K3','Qwen 3.8 Max','Gemini 3.1 Pro','MiniMax M3'];
  const baselines=new Map(mainRows.filter(r=>r[0]==='Single-view').map(r=>[r[1],r[6]]));
  const data=mainRows.filter(r=>r[0]!=='Single-view').map(r=>({
    setting:r[0],agent:r[1],x:r[6],y:(r[6]/baselines.get(r[1])-1)*100,baseline:baselines.get(r[1])
  }));
  const W=960,H=560,m={l:76,r:10,t:8,b:60},plotW=W-m.l-m.r,plotH=H-m.t-m.b;
  const xMin=.41,xMax=1.0,yMin=-14.5,yMax=45;
  const sx=x=>m.l+(x-xMin)/(xMax-xMin)*plotW;
  const sy=y=>m.t+(yMax-y)/(yMax-yMin)*plotH;
  const xTicks=[.5,.6,.7,.8,.9,1.0],yTicks=[-10,0,10,20,30,40];
  const activeSettings=new Set(settings.map(s=>s.name));
  let selectedAgent=null;
  let hoveredAgent=null;

  const labelOffsets={
    'Multi-view|Fable 5':[8,-8],'Multi-view|Opus 5':[-58,13],'Multi-view|GPT-5.6 Sol':[-80,0],
    'Multi-view|Kimi K3':[-55,-8],'Multi-view|Qwen 3.8 Max':[8,8],'Multi-view|Gemini 3.1 Pro':[8,16],
    'Multi-view|MiniMax M3':[10,-12],
    'Active Visual|Fable 5':[10,2],'Active Visual|Opus 5':[-52,-6],'Active Visual|GPT-5.6 Sol':[-84,5],
    'Active Visual|Kimi K3':[-60,4],'Active Visual|Qwen 3.8 Max':[9,3],'Active Visual|Gemini 3.1 Pro':[-92,-10],
    'Active Visual|MiniMax M3':[10,-10],
    'Full 3D Interaction|Fable 5':[10,2],'Full 3D Interaction|Opus 5':[6,-8],'Full 3D Interaction|GPT-5.6 Sol':[10,3],
    'Full 3D Interaction|Kimi K3':[-55,-6],'Full 3D Interaction|Qwen 3.8 Max':[10,4],'Full 3D Interaction|Gemini 3.1 Pro':[10,7],
    'Full 3D Interaction|MiniMax M3':[10,-10]
  };

  function el(name,attrs={},text=''){
    const node=document.createElementNS(NS,name);
    Object.entries(attrs).forEach(([k,v])=>node.setAttribute(k,String(v)));
    if(text)node.textContent=text;
    return node;
  }
  function add(parent,node){parent.appendChild(node);return node}
  function settingMeta(name){return settings.find(s=>s.name===name)}
  function formatPct(v){return `${v>=0?'+':''}${v.toFixed(1)}%`}

  // Plot background + grid.
  const grid=add(svg,el('g',{'aria-hidden':'true'}));
  xTicks.forEach(t=>{
    const x=sx(t); add(grid,el('line',{x1:x,y1:m.t,x2:x,y2:H-m.b,class:'uni3d-grid'}));
    add(grid,el('line',{x1:x,y1:H-m.b,x2:x,y2:H-m.b+5,class:'uni3d-axis'}));
    add(grid,el('text',{x,y:H-m.b+24,'text-anchor':'middle',class:'uni3d-tick'},t.toFixed(1)));
  });
  yTicks.forEach(t=>{
    const y=sy(t); add(grid,el('line',{x1:m.l,y1:y,x2:W-m.r,y2:y,class:t===0?'uni3d-zero':'uni3d-grid'}));
    add(grid,el('line',{x1:m.l-5,y1:y,x2:m.l,y2:y,class:'uni3d-axis'}));
    add(grid,el('text',{x:m.l-10,y:y+5,'text-anchor':'end',class:'uni3d-tick'},String(t)));
  });
  add(grid,el('line',{x1:m.l,y1:m.t,x2:m.l,y2:H-m.b,class:'uni3d-axis'}));
  add(grid,el('line',{x1:m.l,y1:H-m.b,x2:W-m.r,y2:H-m.b,class:'uni3d-axis'}));
  add(grid,el('text',{x:m.l+plotW/2,y:H-12,'text-anchor':'middle',class:'uni3d-axis-label'},'Absolute UNI3D ↑'));
  const yLabel=add(grid,el('text',{x:18,y:m.t+plotH/2,'text-anchor':'middle',class:'uni3d-axis-label'},'Relative Improvement over Single-view (%)'));
  yLabel.setAttribute('transform',`rotate(-90 18 ${m.t+plotH/2})`);

  const marksLayer=add(svg,el('g',{class:'uni3d-marks'}));
  const markNodes=[];
  data.forEach(d=>{
    const meta=settingMeta(d.setting),x=sx(d.x),y=sy(d.y);
    const g=add(marksLayer,el('g',{class:'uni3d-mark',tabindex:'0',role:'button','aria-label':`${d.agent}, ${d.setting}: Uni3D ${d.x.toFixed(3)}, ${formatPct(d.y)} over Single-view`,'data-setting':d.setting,'data-agent':d.agent}));
    let symbol;
    if(meta.shape==='circle')symbol=el('circle',{cx:x,cy:y,r:5.4,fill:meta.color,class:'point-symbol'});
    if(meta.shape==='square')symbol=el('rect',{x:x-5.3,y:y-5.3,width:10.6,height:10.6,rx:.5,fill:meta.color,class:'point-symbol'});
    if(meta.shape==='triangle')symbol=el('path',{d:`M ${x} ${y-6.2} L ${x-5.8} ${y+5.1} L ${x+5.8} ${y+5.1} Z`,fill:meta.color,class:'point-symbol'});
    add(g,symbol);
    const [dx,dy]=labelOffsets[`${d.setting}|${d.agent}`]||[8,-8];
    add(g,el('text',{x:x+dx,y:y+dy,class:'uni3d-label'},d.agent));
    const hit=add(g,el('circle',{cx:x,cy:y,r:14,fill:'transparent','pointer-events':'all'}));
    const show=(event)=>{hoveredAgent=d.agent;updateFocus();showTooltip(event,d)};
    const hide=()=>{hoveredAgent=null;updateFocus();hideTooltip()};
    hit.addEventListener('pointerenter',show); hit.addEventListener('pointermove',e=>positionTooltip(e)); hit.addEventListener('pointerleave',hide);
    hit.addEventListener('click',e=>{e.stopPropagation();selectedAgent=selectedAgent===d.agent?null:d.agent;updateFocus();showTooltip(e,d)});
    g.addEventListener('focus',()=>{hoveredAgent=d.agent;updateFocus()});
    g.addEventListener('blur',()=>{hoveredAgent=null;updateFocus();hideTooltip()});
    g.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();selectedAgent=selectedAgent===d.agent?null:d.agent;updateFocus();}});
    markNodes.push({g,d});
  });

  settings.forEach(meta=>{
    const btn=document.createElement('button');
    btn.type='button'; btn.className='uni3d-setting-btn'; btn.dataset.setting=meta.name; btn.setAttribute('aria-pressed','true');
    const marker=document.createElement('span'); marker.className=`uni3d-setting-marker ${meta.shape}`; marker.style.color=meta.color; if(meta.shape!=='triangle')marker.style.background=meta.color;
    const label=document.createElement('span'); label.textContent=meta.name;
    btn.append(marker,label); legend.appendChild(btn);
    btn.addEventListener('click',()=>{
      if(activeSettings.has(meta.name)&&activeSettings.size===1)return;
      activeSettings.has(meta.name)?activeSettings.delete(meta.name):activeSettings.add(meta.name);
      btn.setAttribute('aria-pressed',String(activeSettings.has(meta.name))); updateFocus(); hideTooltip();
    });
  });

  agents.forEach(agent=>{
    const btn=document.createElement('button'); btn.type='button'; btn.className='uni3d-agent-btn'; btn.textContent=agent; btn.dataset.agent=agent; btn.setAttribute('aria-pressed','false'); agentFilter.appendChild(btn);
    btn.addEventListener('pointerenter',()=>{hoveredAgent=agent;updateFocus()});
    btn.addEventListener('pointerleave',()=>{hoveredAgent=null;updateFocus()});
    btn.addEventListener('focus',()=>{hoveredAgent=agent;updateFocus()});
    btn.addEventListener('blur',()=>{hoveredAgent=null;updateFocus()});
    btn.addEventListener('click',()=>{selectedAgent=selectedAgent===agent?null:agent;updateFocus();});
  });

  function updateFocus(){
    const focusAgent=hoveredAgent||selectedAgent;
    markNodes.forEach(({g,d})=>{
      const visible=activeSettings.has(d.setting);
      g.classList.toggle('is-hidden',!visible);
      g.classList.toggle('is-dimmed',visible&&Boolean(focusAgent)&&d.agent!==focusAgent);
      g.classList.toggle('is-focused',visible&&Boolean(focusAgent)&&d.agent===focusAgent);
    });
    agentFilter.querySelectorAll('.uni3d-agent-btn').forEach(btn=>{
      const active=btn.dataset.agent===selectedAgent;
      const dim=Boolean(focusAgent)&&btn.dataset.agent!==focusAgent;
      btn.setAttribute('aria-pressed',String(active)); btn.classList.toggle('is-dimmed',dim);
    });
  }

  function showTooltip(event,d){
    tooltip.innerHTML=`<b>${d.agent}</b><br><span class="tooltip-setting">${d.setting}</span><br><span class="tooltip-value">Uni3D: <b>${d.x.toFixed(3)}</b> · Single-view: ${d.baseline.toFixed(3)}<br>Improvement: <b>${formatPct(d.y)}</b></span>`;
    tooltip.hidden=false; positionTooltip(event);
  }
  function positionTooltip(event){
    if(tooltip.hidden||!event||typeof event.clientX!=='number')return;
    const r=root.getBoundingClientRect(); let left=event.clientX-r.left+12,top=event.clientY-r.top+12;
    const maxLeft=Math.max(4,r.width-tooltip.offsetWidth-4); left=Math.min(Math.max(4,left),maxLeft);
    if(top+tooltip.offsetHeight>root.clientHeight)top=Math.max(4,event.clientY-r.top-tooltip.offsetHeight-12);
    tooltip.style.left=`${left}px`; tooltip.style.top=`${top}px`;
  }
  function hideTooltip(){tooltip.hidden=true}

  resetBtn?.addEventListener('click',()=>{
    settings.forEach(s=>activeSettings.add(s.name));
    legend.querySelectorAll('.uni3d-setting-btn').forEach(b=>b.setAttribute('aria-pressed','true'));
    selectedAgent=null; hoveredAgent=null; hideTooltip(); updateFocus();
  });
  root.addEventListener('pointerleave',()=>{if(hoveredAgent){hoveredAgent=null;updateFocus()} hideTooltip()});
  updateFocus();
}

initUni3DHarnessChart();

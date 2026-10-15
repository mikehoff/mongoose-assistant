"""The owner CCTV page (car-park camera). Served at /cctv with its script at /cctv/app.js.

The script is a separate file (no inline JS) and builds every element with DOM calls and
textContent, so no recording name is ever interpolated into HTML.

Phone first: it opens on a summary for the chosen day (live tile, a 24 hour timeline, then the
people events as cards). Tapping anything plays it in the stage at the top.
"""
from __future__ import annotations

_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>CCTV: car park</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#16140F;color:#ECE6DA;font:16px/1.45 system-ui,-apple-system,sans-serif;-webkit-text-size-adjust:100%}
header{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:10px 12px;padding-top:max(10px,env(safe-area-inset-top));background:#1F1C16;border-bottom:1px solid #322D24;position:sticky;top:0;z-index:3}
header h1{font-size:18px;margin:0 6px 0 0}
a.back{color:#C9A35B;text-decoration:none;font-weight:600;padding:6px 4px}
.daynav{display:flex;gap:6px;align-items:center;flex:1 1 auto;min-width:0}
.daynav select{flex:1 1 auto;min-width:0;max-width:260px}
select,button,a.btn,input{font:inherit;font-size:16px;background:#2A261E;color:#ECE6DA;border:1px solid #433C30;border-radius:10px;padding:8px 12px;text-decoration:none;cursor:pointer;min-height:42px}
a.btn{display:inline-flex;align-items:center}
button.on{background:#C9A35B;color:#16140F;border-color:#C9A35B;font-weight:700}
button:disabled{opacity:.4;cursor:default}
main{padding:10px 12px 40px;max-width:1100px;margin:0 auto}
h2{font-size:15px;color:#9C9486;margin:16px 0 6px;font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.muted{color:#9C9486}
.msg{padding:10px 12px;border-radius:10px;background:#1F1C16;border:1px solid #322D24;margin:8px 0}
.msg.err{border-color:#8A3B2E;color:#F0B4A8}
#stage{display:none;margin-bottom:10px}
#stage.open{display:block}
.player{background:#000;border-radius:12px;overflow:hidden;position:relative}
.player video,.player img{display:block;width:100%;max-height:72vh;background:#000;object-fit:contain}
.bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:8px 0}
.bar .sep{flex:1 1 auto}
.info{font-weight:600;margin:4px 0}
.strip{display:flex;gap:8px;overflow-x:auto;padding:4px 0 8px;-webkit-overflow-scrolling:touch}
.strip img{height:110px;min-width:60px;border-radius:8px;background:#000;cursor:zoom-in;flex:0 0 auto}
.livetile{position:relative;max-width:560px;border-radius:12px;overflow:hidden;background:#000;cursor:pointer;border:1px solid #322D24}
.livetile img{display:block;width:100%;aspect-ratio:16/9;object-fit:cover}
.livetile .tag{position:absolute;left:8px;top:8px;background:#B3412F;color:#fff;font-weight:700;font-size:13px;padding:2px 8px;border-radius:6px}
.livetile .hint{position:absolute;left:0;right:0;bottom:0;padding:6px 10px;background:linear-gradient(transparent,rgba(0,0,0,.75));font-size:14px}
.tlwrap{margin:6px 0 4px}
.tl{position:relative;height:46px;background:#24201A;border:1px solid #322D24;border-radius:8px;cursor:pointer;touch-action:none;overflow:visible;margin-top:22px}
.tltip{display:none;position:absolute;top:-24px;transform:translateX(-50%);background:#F2E6CF;color:#1A1712;font:600 13px system-ui;padding:2px 6px;border-radius:6px;pointer-events:none;white-space:nowrap}
.tl .rng{position:absolute;top:12px;bottom:12px;background:#5C7F4F}
.tl .rng.old{background:#6A6253}
.tl .ev{position:absolute;top:3px;bottom:3px;width:3px;margin-left:-1px;background:#E0714F;border-radius:2px}
.tl .ev.mo{background:#5B9BD5}
.tl .ph{position:absolute;top:0;bottom:0;width:2px;margin-left:-1px;background:#fff;display:none}
.ticks{position:relative;height:18px;font-size:12px;color:#9C9486}
.ticks span{position:absolute;transform:translateX(-50%)}
.ticks span:first-child{transform:none}.ticks span:last-child{transform:translateX(-100%)}
.legend{font-size:13px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin:0 4px 0 10px;vertical-align:middle}
form.goto{display:flex;gap:6px;align-items:center;margin:8px 0}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:14px 0 6px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.card{background:#1F1C16;border:1px solid #322D24;border-radius:12px;overflow:hidden;cursor:pointer}
.card.sel{border-color:#C9A35B;box-shadow:0 0 0 1px #C9A35B}
.card img,.card .noimg{width:100%;display:block;aspect-ratio:16/9;object-fit:cover;background:#000}
.card .noimg{display:flex;align-items:center;justify-content:center;color:#9C9486;font-size:13px}
.card .t{font-weight:700;font-size:18px;padding:6px 8px 0;font-variant-numeric:tabular-nums}
.card.mo .t{color:#A9C8EA}
.badge{display:inline-block;margin-left:6px;font-size:11px;font-weight:700;vertical-align:middle;background:#2D4A66;color:#CFE2F5;border-radius:6px;padding:1px 6px;letter-spacing:.02em}
.card .s{padding:0 8px 8px;font-size:14px;color:#9C9486}
.rows{display:flex;flex-direction:column;gap:6px}
.row{background:#1F1C16;border:1px solid #322D24;border-radius:10px;padding:10px 12px;cursor:pointer;display:flex;justify-content:space-between;gap:10px;font-variant-numeric:tabular-nums}
.row.sel{border-color:#C9A35B}
#lb{position:fixed;inset:0;background:rgba(0,0,0,.94);z-index:10;display:none;flex-direction:column}
#lb.open{display:flex}
#lb .top{display:flex;gap:6px;align-items:center;padding:8px;padding-top:max(8px,env(safe-area-inset-top))}
#lb .top .sep{flex:1 1 auto}
#lb .pic{flex:1 1 auto;display:flex;align-items:center;justify-content:center;min-height:0;touch-action:pan-y pinch-zoom}
#lb .pic img{max-width:100%;max-height:100%;object-fit:contain}
#lb .bot{display:flex;gap:6px;justify-content:space-between;align-items:center;padding:8px;padding-bottom:max(8px,env(safe-area-inset-bottom))}
.keys{font-size:13px;margin-top:18px}
@media (hover:none){.keys{display:none}}
</style></head>
<body>
<header><a class="back" href="/ui#rtm" id="back">Back</a><h1>CCTV</h1>
<div class="daynav"><button id="dprev" aria-label="Previous day">&lsaquo;</button>
<select id="day" aria-label="Day"><option value="">Loading</option></select>
<button id="dnext" aria-label="Next day">&rsaquo;</button></div></header>
<main>
<noscript><div class="msg err">This page needs JavaScript. The live picture is at <a href="/cctv/live/latest.jpg">/cctv/live/latest.jpg</a>.</div></noscript>
<div id="status" class="msg">Loading recordings.</div>
<section id="stage">
<div class="player" id="player"></div>
<div class="info" id="info"></div>
<div class="bar" id="ctl"></div>
<div class="strip" id="strip"></div>
</section>
<section id="summary">
<div class="livetile" id="livetile" role="button" tabindex="0" aria-label="Play live video">
<img id="liveimg" alt="Latest camera picture"><span class="tag">LIVE</span><span class="hint" id="livehint">Tap to play live</span></div>
<div class="bar"><button id="snap">Snapshot</button></div>
<h2>Day timeline</h2>
<div class="tlwrap"><div class="tl" id="tl" role="button" aria-label="Day timeline, tap a time to play it"></div>
<div class="ticks" id="ticks"></div>
<div class="legend muted"><i style="background:#5C7F4F;margin-left:0"></i>Recording<i style="background:#6A6253"></i>Older<i style="background:#E0714F"></i>Person<i style="background:#5B9BD5"></i>Movement</div></div>
<form class="goto" id="goto"><label class="muted" for="gotime">Go to time</label>
<input type="time" id="gotime" step="1"><button type="submit">Go</button></form>
<div class="tabs" id="tabs"></div>
<div id="list"></div>
<div class="keys muted">Keys: space play or pause, left and right arrows skip 10 s, j and k previous and next, Esc closes.</div>
</section>
</main>
<div id="lb" role="dialog" aria-label="Picture">
<div class="top"><span id="lbcap"></span><span class="sep"></span><a class="btn" id="lbdl" href="#" download>Download</a><button id="lbclose">Close</button></div>
<div class="pic" id="lbpic"><img id="lbimg" alt=""></div>
<div class="bot"><button id="lbprev">Previous</button><span id="lbcount" class="muted"></span><button id="lbnext">Next</button></div>
</div>
<script src="/cctv/app.js"></script>
</body></html>
"""

CCTV_JS = r"""
(function(){
'use strict';
var STORE='mongoose.cctv.view';
var TABS=[['person','People'],['motion','Movement'],['continuous','Continuous'],['older','Older']];
var SPEEDS=[1,2,4,8];
var state={day:'',days:[],info:{},data:null,tab:'person',cur:null,rate:1,video:null,
  liveTimer:null,tileTimer:null,refreshTimer:null,lastSave:0,lb:{items:[],i:0},restore:null};
var el=function(id){return document.getElementById(id);};
var isSafariHls=(function(){var v=document.createElement('video');return !!(v.canPlayType&&v.canPlayType('application/vnd.apple.mpegurl'));})();

function mk(tag,cls,text){var n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined&&text!==null)n.textContent=text;return n;}
function clear(node){while(node.firstChild)node.removeChild(node.firstChild);}
function btn(label,fn,title){var b=mk('button','',label);b.type='button';if(title){b.title=title;b.setAttribute('aria-label',title);}b.addEventListener('click',fn);return b;}
function setStatus(text,isErr){var s=el('status');s.textContent=text||'';s.className='msg'+(isErr?' err':'');s.style.display=text?'block':'none';}
function pad2(n){return (n<10?'0':'')+n;}
function hms(sec){sec=Math.max(0,Math.floor(sec));return pad2(Math.floor(sec/3600)%24)+':'+pad2(Math.floor(sec/60)%60)+':'+pad2(sec%60);}
function fmtDur(s,type){if(s===null||s===undefined)return '';var w=type==='motion'?'movement':'seen';if(s<2)return w+' briefly';if(s<60)return w+' about '+s+' s';var m=Math.floor(s/60),r=s%60;return w+' about '+m+' min'+(r?' '+r+' s':'');}
function typeName(t){return t==='motion'?'Movement':'Person';}
function fmtSize(n){return n>1048576?(n/1048576).toFixed(0)+' MB':Math.max(1,Math.round(n/1024))+' KB';}
function fileUrl(kind,name){return '/cctv/file/'+kind+'/'+encodeURIComponent(name);}
function todayStr(){var d=new Date();return d.getFullYear()+pad2(d.getMonth()+1)+pad2(d.getDate());}
function fmtDay(d){return d.slice(6,8)+'/'+d.slice(4,6)+'/'+d.slice(0,4);}
function dayLabel(d){
  var dt=new Date(+d.slice(0,4),+d.slice(4,6)-1,+d.slice(6,8));
  var wd=['Sun','Mon','Tue','Wed','Thu','Fri','Sat'][dt.getDay()];
  var base=(d===todayStr()?'Today':wd)+' '+d.slice(6,8)+'/'+d.slice(4,6);
  var inf=state.info[d];
  if(inf){var bits=[];if(inf.events)bits.push(inf.events+(inf.events===1?' person':' people'));if(inf.motion)bits.push(inf.motion+' movement');
    base+=' ('+(bits.length?bits.join(', '):'no people')+')';}
  return base;
}

function getJson(url){
  var ctl=('AbortController' in window)?new AbortController():null;
  var t=setTimeout(function(){if(ctl)ctl.abort();},15000);
  return fetch(url,{credentials:'same-origin',cache:'no-store',signal:ctl?ctl.signal:undefined}).then(function(r){
    clearTimeout(t);
    if(r.status===403)throw new Error('This page is just for the owner. Open it from the Cockpit or your own phone.');
    if(!r.ok)throw new Error('The server answered '+r.status+'.');
    return r.json();
  },function(e){clearTimeout(t);throw new Error(e&&e.name==='AbortError'?'The server took too long to answer.':'Could not reach Mongoose.');});
}

/* ---------- remembered view ---------- */
function saveView(t){
  var c=state.cur,v={day:state.day,tab:state.tab};
  if(c&&c.kind!=='live'){v.kind=c.kind;v.name=c.name;v.t=(typeof t==='number')?t:(state.video?state.video.currentTime:0);}
  try{localStorage.setItem(STORE,JSON.stringify(v));}catch(e){}
}
function loadView(){try{var v=JSON.parse(localStorage.getItem(STORE)||'null');return (v&&typeof v==='object')?v:null;}catch(e){return null;}}

/* ---------- data views ---------- */
function events(){return (state.data&&state.data.events)||[];}
function evList(type){return events().filter(function(e){return (e.type||'person')===type;});}
function segs(kind){return (state.data&&state.data[kind])||[];}
function eventStills(e){
  var out=[];
  (e.stills||[]).forEach(function(s){
    if(s.full)out.push({name:s.full,label:'Picture '+s.index});
    if(s.body)out.push({name:s.body,label:'Body '+s.index});
    if(s.face)out.push({name:s.face,label:'Face '+s.index});
  });
  return out;
}
function eventThumb(e){
  var st=e.stills||[];
  for(var i=0;i<st.length;i++){if(st[i].full)return st[i].full;}
  for(var j=0;j<st.length;j++){if(st[j].body)return st[j].body;}
  return '';
}

/* ---------- stage (the big player) ---------- */
function stopLive(){if(state.liveTimer){clearInterval(state.liveTimer);state.liveTimer=null;}}
function stopVideo(){
  var v=state.video;state.video=null;
  if(v){if(v._metaTimer)clearTimeout(v._metaTimer);try{v.pause();v.removeAttribute('src');v.load();}catch(e){}}
}
function resetStage(){stopLive();stopVideo();clear(el('player'));clear(el('ctl'));clear(el('strip'));el('info').textContent='';el('tl').querySelector('.ph').style.display='none';}
function openStage(){el('stage').className='open';}
function closeStage(){resetStage();state.cur=null;el('stage').className='';markSelected();setStatus('');saveView();startTile();}
function scrollToStage(){try{el('stage').scrollIntoView({behavior:'smooth',block:'start'});}catch(e){window.scrollTo(0,0);}}

function makeVideo(url,opts){
  var v=document.createElement('video');
  v.controls=true;v.playsInline=true;v.setAttribute('playsinline','');v.preload='metadata';v.muted=true;
  v._metaTimer=setTimeout(function(){if(state.video===v&&!(v.readyState>=1))setStatus('This recording is taking too long to load. Try again, or use Download to save it.',true);},20000);
  v.addEventListener('loadedmetadata',function(){clearTimeout(v._metaTimer);
    if(opts.seek>0){var d=isFinite(v.duration)?v.duration:opts.seek;try{v.currentTime=Math.max(0,Math.min(opts.seek,d-0.5));}catch(e){}}
    if(opts.rate){v.defaultPlaybackRate=state.rate;v.playbackRate=state.rate;}
    if(opts.autoplay){var pr=v.play();if(pr&&pr.catch)pr.catch(function(){});}
  });
  v.addEventListener('error',function(){if(state.video!==v)return;clearTimeout(v._metaTimer);
    setStatus('This recording would not play here. It is HEVC video, which plays in Safari. Use Download to save it.',true);});
  v.src=url;state.video=v;el('player').appendChild(v);
  return v;
}

function skip(d){var v=state.video;if(!v)return;try{var t=v.currentTime+d;if(isFinite(v.duration))t=Math.min(t,v.duration-0.1);v.currentTime=Math.max(0,t);}catch(e){}}
function togglePlay(){var v=state.video;if(!v)return;if(v.paused){var pr=v.play();if(pr&&pr.catch)pr.catch(function(){});}else{v.pause();}}
function setRate(r){state.rate=r;if(state.video){state.video.defaultPlaybackRate=r;state.video.playbackRate=r;}
  var bs=el('ctl').querySelectorAll('button[data-rate]');for(var i=0;i<bs.length;i++){bs[i].className=(+bs[i].getAttribute('data-rate')===r)?'on':'';}}
function dlLink(url,name,label){var a=mk('a','btn',label||'Download');a.href=url;a.setAttribute('download',name||'');return a;}

function showImageFeed(){
  var p=el('player');clear(p);stopVideo();
  var img=document.createElement('img');img.alt='Latest camera picture';
  var fails=0;
  img.onerror=function(){fails++;if(fails>=3)setStatus('The live picture is not available. The recorder may be stopped.',true);};
  img.onload=function(){fails=0;setStatus('');};
  var tick=function(){img.src='/cctv/live/latest.jpg?t='+Date.now();};
  tick();stopLive();state.liveTimer=setInterval(tick,1000);
  p.appendChild(img);
}

function playLive(){
  resetStage();stopTile();openStage();state.cur={kind:'live'};markSelected();
  el('info').textContent='Live';
  var ctl=el('ctl');ctl.appendChild(btn('Snapshot',openSnapshot));ctl.appendChild(mk('span','sep'));ctl.appendChild(btn('Close',closeStage));
  scrollToStage();
  if(!isSafariHls){setStatus('Showing the latest picture, refreshed every second. Full live video plays in Safari.');showImageFeed();return;}
  setStatus('Connecting to the live feed.');
  var v=document.createElement('video');v.controls=true;v.autoplay=true;v.muted=true;v.playsInline=true;v.setAttribute('playsinline','');
  var settled=false;
  var giveUp=setTimeout(function(){if(!settled&&state.video===v){settled=true;setStatus('Live video did not start, so showing the latest picture instead.',true);showImageFeed();}},12000);
  v.addEventListener('playing',function(){if(!settled){settled=true;clearTimeout(giveUp);setStatus('');}});
  v.addEventListener('error',function(){if(!settled&&state.video===v){settled=true;clearTimeout(giveUp);setStatus('Live video failed, so showing the latest picture instead.',true);showImageFeed();}});
  v.src='/cctv/live/live.m3u8';state.video=v;
  el('player').appendChild(v);
  var pr=v.play();if(pr&&pr.catch)pr.catch(function(){});
}

function openSnapshot(){
  var ts=Date.now(),d=new Date(ts);
  var name='car-park-'+d.getFullYear()+pad2(d.getMonth()+1)+pad2(d.getDate())+'-'+pad2(d.getHours())+pad2(d.getMinutes())+pad2(d.getSeconds())+'.jpg';
  var url='/cctv/live/latest.jpg?t='+ts;
  openLightbox([{url:url,dl:url,dlname:name,label:'Snapshot at '+pad2(d.getHours())+':'+pad2(d.getMinutes())+':'+pad2(d.getSeconds())}],0);
}

function playEvent(type,i,opts){
  var list=evList(type);if(i<0||i>=list.length)return;
  opts=opts||{};var e=list[i];
  resetStage();stopTile();openStage();state.cur={kind:'event',type:type,idx:i,name:e.clip||(type+'_'+e.stamp)};markSelected();
  var info=typeName(type)+' at '+e.time+(e.seen_s!==null&&e.seen_s!==undefined?', '+fmtDur(e.seen_s,type):'');
  el('info').textContent=info;
  var stills=eventStills(e);
  if(e.clip){
    setStatus('');
    var v=makeVideo(fileUrl('event',e.clip),{autoplay:opts.autoplay!==false,seek:opts.seek||0,rate:false});
    v.addEventListener('timeupdate',throttledSave);
  } else {
    setStatus('This event has pictures but no video clip.');
    if(stills.length){var im=document.createElement('img');im.src=fileUrl('still',stills[0].name);im.alt='Event picture';el('player').appendChild(im);}
  }
  var ctl=el('ctl');
  var bp=btn('Previous',function(){playEvent(type,i-1);},'Previous (j)');bp.disabled=(i<=0);ctl.appendChild(bp);
  var bn=btn('Next',function(){playEvent(type,i+1);},'Next (k)');bn.disabled=(i>=list.length-1);ctl.appendChild(bn);
  if(e.clip){ctl.appendChild(btn('-10 s',function(){skip(-10);},'Back 10 seconds'));ctl.appendChild(btn('+10 s',function(){skip(10);},'Forward 10 seconds'));}
  ctl.appendChild(btn('Recording around this',function(){playAt(Math.max(0,e.start_s-(e.pre_roll_s||10)));},'Play the continuous recording from just before this'));
  if(e.clip)ctl.appendChild(dlLink(fileUrl('event',e.clip)+'?download=1',e.clip));
  ctl.appendChild(mk('span','sep'));ctl.appendChild(btn('Close',closeStage));
  var strip=el('strip');
  var lbItems=stills.map(function(s){var u=fileUrl('still',s.name);return {url:u,dl:u+'?download=1',dlname:s.name,label:typeName(type)+' at '+e.time+': '+s.label};});
  stills.forEach(function(s,k){
    var im=document.createElement('img');im.loading='lazy';im.decoding='async';im.alt=s.label;im.title=s.label;
    im.src=fileUrl('still',s.name)+'?thumb=1';
    im.addEventListener('click',function(){openLightbox(lbItems,k);});
    strip.appendChild(im);
  });
  if(opts.scroll!==false)scrollToStage();
  saveView(opts.seek||0);
}

function segLabel(r){return hms(r.start_s)+' to '+hms(r.end_s)+(r.growing?' (still recording)':'');}

function playSeg(kind,i,offset,opts){
  var list=segs(kind);if(i<0||i>=list.length)return;
  opts=opts||{};var r=list[i];
  resetStage();stopTile();openStage();state.cur={kind:kind,idx:i,name:r.name};markSelected();
  var title=(kind==='older'?'Older recording ':'Recording ')+segLabel(r);
  el('info').textContent=title;
  setStatus('');
  var v=makeVideo(fileUrl(kind,r.name),{autoplay:opts.autoplay!==false,seek:offset||0,rate:true});
  var ph=el('tl').querySelector('.ph');
  v.addEventListener('timeupdate',function(){
    if(state.video!==v)return;
    var at=r.start_s+v.currentTime;
    el('info').textContent=title+'. Showing '+hms(at);
    if(state.day===state.data.day){ph.style.left=(Math.min(at,86400)/864)+'%';ph.style.display='block';}
    throttledSave();
  });
  v.addEventListener('ended',function(){
    if(state.video!==v)return;
    if(i+1<list.length){playSeg(kind,i+1,0,{autoplay:true,scroll:false});setStatus('Playing the next recording from '+list[i+1].time+'.');}
    else setStatus('That was the last recording of the day.');
  });
  var ctl=el('ctl');
  SPEEDS.forEach(function(s){var b=btn(s+'x',function(){setRate(s);},'Play at '+s+' times speed');b.setAttribute('data-rate',String(s));ctl.appendChild(b);});
  ctl.appendChild(btn('-10 s',function(){skip(-10);},'Back 10 seconds'));
  ctl.appendChild(btn('+10 s',function(){skip(10);},'Forward 10 seconds'));
  var bp=btn('Previous',function(){playSeg(kind,i-1,0);},'Previous recording (j)');bp.disabled=(i<=0);ctl.appendChild(bp);
  var bn=btn('Next',function(){playSeg(kind,i+1,0);},'Next recording (k)');bn.disabled=(i>=list.length-1);ctl.appendChild(bn);
  ctl.appendChild(dlLink(fileUrl(kind,r.name)+'?download=1',r.name));
  ctl.appendChild(mk('span','sep'));ctl.appendChild(btn('Close',closeStage));
  setRate(state.rate);
  if(opts.scroll!==false)scrollToStage();
  saveView(offset||0);
}

function throttledSave(){var now=Date.now();if(now-state.lastSave>4000){state.lastSave=now;saveView();}}

/* Play whatever was recording at sec (seconds after midnight): continuous first, then older. */
function playAt(sec){
  var kinds=['continuous','older'],best=null;
  for(var k=0;k<kinds.length;k++){
    var list=segs(kinds[k]);
    for(var i=0;i<list.length;i++){
      var r=list[i];
      if(sec>=r.start_s&&sec<r.end_s){playSeg(kinds[k],i,sec-r.start_s);return;}
      var dist=sec<r.start_s?r.start_s-sec:sec-r.end_s;
      if(!best||dist<best.dist)best={kind:kinds[k],i:i,dist:dist,before:sec<r.start_s};
    }
  }
  if(!best){setStatus('No video was recorded on '+fmtDay(state.day)+'.',true);return;}
  var b=segs(best.kind)[best.i];
  var off=best.before?0:Math.max(0,b.end_s-b.start_s-5);
  playSeg(best.kind,best.i,off);
  setStatus('Nothing was recorded at '+hms(sec)+'. Playing the nearest recording, from '+hms(b.start_s+off)+'.');
}

/* ---------- picture viewer ---------- */
function openLightbox(items,i){state.lb={items:items,i:0};el('lb').className='open';showLb(i);}
function closeLightbox(){el('lb').className='';el('lbimg').removeAttribute('src');}
function lbOpen(){return el('lb').className==='open';}
function showLb(i){
  var lb=state.lb,n=lb.items.length;if(!n)return;
  i=(i+n)%n;lb.i=i;var it=lb.items[i];
  var img=el('lbimg');img.alt=it.label;
  img.onerror=function(){el('lbcap').textContent='This picture would not load.';};
  img.src=it.url;
  el('lbcap').textContent=it.label;
  el('lbcount').textContent=n>1?(i+1)+' of '+n:'';
  var a=el('lbdl');a.href=it.dl;a.setAttribute('download',it.dlname||'');
  el('lbprev').disabled=n<2;el('lbnext').disabled=n<2;
}

/* ---------- summary: live tile, timeline, lists ---------- */
function tick(){var im=el('liveimg');im.src='/cctv/live/latest.jpg?t='+Date.now();}
function stopTile(){if(state.tileTimer){clearInterval(state.tileTimer);state.tileTimer=null;}}
function startTile(){stopTile();if(document.hidden)return;tick();state.tileTimer=setInterval(function(){if(!document.hidden)tick();},10000);}

function renderTimeline(){
  var tl=el('tl');clear(tl);
  [['continuous','rng'],['older','rng old']].forEach(function(p){
    segs(p[0]).forEach(function(r){var d=mk('div',p[1]);d.style.left=(r.start_s/864)+'%';d.style.width=(Math.max(r.end_s-r.start_s,60)/864)+'%';tl.appendChild(d);});
  });
  events().forEach(function(e){var t=e.type||'person';var d=mk('div','ev'+(t==='motion'?' mo':''));d.style.left=(e.start_s/864)+'%';d.title=typeName(t)+' at '+e.time;tl.appendChild(d);});
  tl.appendChild(mk('div','ph'));
}

function renderTabs(){
  var tabs=el('tabs');clear(tabs);
  TABS.forEach(function(t){
    var n=state.data?segsOrEvents(t[0]).length:0;
    var b=btn(t[1]+' ('+n+')',function(){state.tab=t[0];renderTabs();renderList();saveView();});
    b.className=state.tab===t[0]?'on':'';tabs.appendChild(b);
  });
}
function segsOrEvents(tab){return (tab==='person'||tab==='motion')?evList(tab):segs(tab);}

function renderList(){
  var list=el('list');clear(list);
  var items=segsOrEvents(state.tab);
  var names={person:'people',motion:'movement events',continuous:'continuous recordings',older:'older recordings'};
  if(!items.length){list.appendChild(mk('div','msg','No '+names[state.tab]+' on '+fmtDay(state.day)+'.'));return;}
  if(state.tab==='person'||state.tab==='motion'){
    var type=state.tab,g=mk('div','grid');
    items.forEach(function(e,i){
      var c=mk('div','card'+(type==='motion'?' mo':''));c.tabIndex=0;c.setAttribute('role','button');c.setAttribute('data-key',type+':'+i);
      var th=eventThumb(e);
      if(th){var im=document.createElement('img');im.loading='lazy';im.decoding='async';im.alt='';im.src=fileUrl('still',th)+'?thumb=1';
        im.onerror=function(){var ph=mk('div','noimg','No picture');if(im.parentNode)im.parentNode.replaceChild(ph,im);};c.appendChild(im);}
      else c.appendChild(mk('div','noimg','No picture'));
      var tl=mk('div','t',e.time);if(type==='motion')tl.appendChild(mk('span','badge','Movement'));c.appendChild(tl);
      var sub=e.seen_s!==null&&e.seen_s!==undefined?fmtDur(e.seen_s,type):'';
      if(!e.clip)sub=(sub?sub+', ':'')+'pictures only';
      c.appendChild(mk('div','s',sub||' '));
      c.addEventListener('click',function(){playEvent(type,i);});
      c.addEventListener('keydown',function(ev){if(ev.key==='Enter'){ev.preventDefault();playEvent(type,i);}});
      g.appendChild(c);
    });
    list.appendChild(g);
  } else {
    var kind=state.tab,rows=mk('div','rows');
    items.forEach(function(r,i){
      var row=mk('div','row');row.tabIndex=0;row.setAttribute('role','button');row.setAttribute('data-key',kind+':'+i);
      row.appendChild(mk('span','',segLabel(r)));row.appendChild(mk('span','muted',fmtSize(r.size)));
      row.addEventListener('click',function(){playSeg(kind,i,0);});
      row.addEventListener('keydown',function(ev){if(ev.key==='Enter'){ev.preventDefault();playSeg(kind,i,0);}});
      rows.appendChild(row);
    });
    list.appendChild(rows);
  }
  markSelected();
}

function markSelected(){
  var c=state.cur,key=(c&&c.kind!=='live')?(c.kind==='event'?c.type:c.kind)+':'+c.idx:'';
  var nodes=el('list').querySelectorAll('[data-key]');
  for(var i=0;i<nodes.length;i++){var base=nodes[i].className.replace(/ sel$/,'');nodes[i].className=base+(nodes[i].getAttribute('data-key')===key?' sel':'');}
}

function renderDay(){
  renderTimeline();renderTabs();renderList();
  var ev=events().length,co=segs('continuous').length,ol=segs('older').length;
  if(!ev&&!co&&!ol)setStatus('Nothing was recorded on '+fmtDay(state.day)+'.');
  else if(!(state.cur&&state.cur.kind==='live'))setStatus('');
}

function renderDays(){
  var sel=el('day');clear(sel);
  if(!state.days.length){var o=mk('option','','No recordings yet');o.value='';sel.appendChild(o);}
  state.days.forEach(function(d){var o=mk('option','',dayLabel(d));o.value=d;sel.appendChild(o);});
  sel.value=state.day;
  var i=state.days.indexOf(state.day);
  el('dprev').disabled=(i<0||i>=state.days.length-1);
  el('dnext').disabled=(i<=0);
}

function loadDay(){
  if(!state.day){state.data={day:'',events:[],continuous:[],older:[]};renderDay();return Promise.resolve();}
  setStatus('Loading '+fmtDay(state.day)+'.');
  var want=state.day;
  return getJson('/cctv/api/day/'+want).then(function(j){
    if(want!==state.day)return;
    state.data=j;renderDay();
    var rs=state.restore;state.restore=null;
    if(rs&&rs.day===want&&rs.name)restoreSelection(rs);
  },function(e){
    if(want!==state.day)return;
    state.data={day:want,events:[],continuous:[],older:[]};renderTimeline();renderTabs();clear(el('list'));setStatus(e.message,true);
  });
}

function restoreSelection(rs){
  var t=typeof rs.t==='number'&&rs.t>0?rs.t:0;
  if(rs.kind==='event'){var ts=['person','motion'];for(var x=0;x<ts.length;x++){var ev=evList(ts[x]);
    for(var i=0;i<ev.length;i++){if((ev[i].clip||(ts[x]+'_'+ev[i].stamp))===rs.name){playEvent(ts[x],i,{autoplay:false,seek:t,scroll:false});return;}}}}
  else if(rs.kind==='continuous'||rs.kind==='older'){var l=segs(rs.kind);for(var k=0;k<l.length;k++){if(l[k].name===rs.name){playSeg(rs.kind,k,t,{autoplay:false,scroll:false});return;}}}
}

function changeDay(d){
  if(!d||d===state.day)return;
  if(state.cur&&state.cur.kind!=='live')closeStage();
  state.day=d;state.data=null;renderDays();saveView();loadDay();
}
function stepDay(dir){var i=state.days.indexOf(state.day)+dir;if(i>=0&&i<state.days.length)changeDay(state.days[i]);}

/* Today's list refreshes quietly every minute (the stage and the open picture are left alone). */
function refreshToday(){
  if(document.hidden||!state.data||state.day!==todayStr())return;
  var want=state.day;
  getJson('/cctv/api/day/'+want).then(function(j){
    if(want!==state.day||!state.data)return;
    var same=j.events.length===events().length&&j.continuous.length===segs('continuous').length;
    var playing=state.cur&&state.cur.kind!=='live';
    if(same||playing){
      if(!playing){state.data=j;renderTimeline();}
      return;
    }
    state.data=j;renderTimeline();renderTabs();renderList();
    if(state.info[want]){state.info[want].events=evList('person').length;state.info[want].motion=evList('motion').length;renderDays();}
  },function(){});
}

/* ---------- keyboard (desktop) ---------- */
function onKey(ev){
  var t=ev.target,tag=t&&t.tagName?t.tagName.toLowerCase():'';
  if(tag==='input'||tag==='select'||tag==='textarea'||ev.metaKey||ev.ctrlKey||ev.altKey)return;
  if(lbOpen()){
    if(ev.key==='Escape'){closeLightbox();ev.preventDefault();}
    else if(ev.key==='ArrowLeft'){showLb(state.lb.i-1);ev.preventDefault();}
    else if(ev.key==='ArrowRight'){showLb(state.lb.i+1);ev.preventDefault();}
    return;
  }
  var c=state.cur;
  if(ev.key===' '&&tag!=='button'&&tag!=='a'&&tag!=='video'){if(state.video){togglePlay();ev.preventDefault();}}
  else if(ev.key==='ArrowLeft'&&tag!=='video'){if(state.video){skip(-10);ev.preventDefault();}}
  else if(ev.key==='ArrowRight'&&tag!=='video'){if(state.video){skip(10);ev.preventDefault();}}
  else if(ev.key==='j'||ev.key==='k'){
    var d=ev.key==='j'?-1:1;
    if(c&&c.kind==='event')playEvent(c.type,c.idx+d);
    else if(c&&(c.kind==='continuous'||c.kind==='older'))playSeg(c.kind,c.idx+d,0);
    else if(state.tab==='person'||state.tab==='motion')playEvent(state.tab,d>0?0:evList(state.tab).length-1);
  }
  else if(ev.key==='Escape'&&c){closeStage();}
}

function init(){
  if(/[?&]from=phone/.test(location.search)){el('back').href='/device/launchpad';}
  var ticks=el('ticks');
  [0,3,6,9,12,15,18,21,24].forEach(function(h){var s=mk('span','',pad2(h));s.style.left=(h/24*100)+'%';ticks.appendChild(s);});
  el('tl').appendChild(mk('div','ph'));
  // Drag along the bar and a time label follows the finger; release to play from that moment. A plain
  // tap still works. Pointer events cover mouse, pen and touch in Safari 13+.
  var tl=el('tl'),tip=mk('div','tltip'),drag=false;
  tl.appendChild(tip);
  function tlSec(ev){var r=tl.getBoundingClientRect();if(!r.width)return null;
    return Math.round(Math.max(0,Math.min(1,(ev.clientX-r.left)/r.width))*86399);}
  function tlShow(ev){var sec=tlSec(ev);if(sec===null)return null;
    tip.textContent=pad2(Math.floor(sec/3600))+':'+pad2(Math.floor(sec%3600/60));
    tip.style.left=(sec/86399*100)+'%';tip.style.display='block';return sec;}
  tl.addEventListener('pointerdown',function(ev){drag=true;try{tl.setPointerCapture(ev.pointerId);}catch(e){}tlShow(ev);ev.preventDefault();});
  tl.addEventListener('pointermove',function(ev){if(drag)tlShow(ev);});
  tl.addEventListener('pointerup',function(ev){if(!drag)return;drag=false;tip.style.display='none';
    var sec=tlSec(ev);if(sec===null)return;
    if(!state.data){setStatus('The day is still loading.');return;}
    playAt(sec);});
  tl.addEventListener('pointercancel',function(){drag=false;tip.style.display='none';});
  el('goto').addEventListener('submit',function(ev){
    ev.preventDefault();
    var m=/^(\d{1,2}):(\d{2})(?::(\d{2}))?/.exec(el('gotime').value||'');
    if(!m){setStatus('Pick a time first, for example 14:30.',true);return;}
    if(!state.data){setStatus('The day is still loading.');return;}
    playAt(+m[1]*3600+ +m[2]*60+(m[3]?+m[3]:0));
  });
  var tile=el('livetile');
  tile.addEventListener('click',playLive);
  tile.addEventListener('keydown',function(ev){if(ev.key==='Enter'){ev.preventDefault();playLive();}});
  var tileFails=0;
  el('liveimg').addEventListener('error',function(){tileFails++;if(tileFails>=2)el('livehint').textContent='Live picture not available. The recorder may be stopped.';});
  el('liveimg').addEventListener('load',function(){tileFails=0;el('livehint').textContent='Tap to play live';});
  el('snap').addEventListener('click',openSnapshot);
  el('dprev').addEventListener('click',function(){stepDay(1);});
  el('dnext').addEventListener('click',function(){stepDay(-1);});
  el('day').addEventListener('change',function(){changeDay(el('day').value);});
  el('lbclose').addEventListener('click',closeLightbox);
  el('lbprev').addEventListener('click',function(){showLb(state.lb.i-1);});
  el('lbnext').addEventListener('click',function(){showLb(state.lb.i+1);});
  el('lbpic').addEventListener('click',function(ev){if(ev.target===el('lbpic'))closeLightbox();});
  var sx=null,sy=0;
  el('lbpic').addEventListener('touchstart',function(ev){if(ev.touches.length===1){sx=ev.touches[0].clientX;sy=ev.touches[0].clientY;}else sx=null;},{passive:true});
  el('lbpic').addEventListener('touchend',function(ev){
    if(sx===null||!ev.changedTouches.length)return;
    var dx=ev.changedTouches[0].clientX-sx,dy=ev.changedTouches[0].clientY-sy;sx=null;
    if(Math.abs(dx)>50&&Math.abs(dx)>Math.abs(dy)*1.5)showLb(state.lb.i+(dx<0?1:-1));
  },{passive:true});
  document.addEventListener('keydown',onKey);
  document.addEventListener('visibilitychange',function(){if(document.hidden)stopTile();else if(!state.cur)startTile();});
  var saved=loadView()||{};
  if(['person','motion','continuous','older'].indexOf(saved.tab)>=0)state.tab=saved.tab;
  renderTabs();
  startTile();
  getJson('/cctv/api/days').then(function(j){
    var info=j.day_info||[];state.info={};
    info.forEach(function(d){state.info[d.day]=d;});
    state.days=j.days||[];
    state.day=(saved.day&&state.days.indexOf(saved.day)>=0)?saved.day:(state.days[0]||'');
    if(saved.day===state.day&&saved.name)state.restore=saved;
    renderDays();
    if(j.status&&j.status.base_exists===false){setStatus('The camera folder was not found on this Mac.',true);return;}
    state.refreshTimer=setInterval(refreshToday,60000);
    return loadDay();
  },function(e){var sel=el('day');clear(sel);var o=mk('option','','Unavailable');sel.appendChild(o);setStatus(e.message,true);});
}
init();
})();
"""


def cctv_html() -> str:
    return _HTML

/* 前三名奖牌 */
var _MDL_ID=0, _MDL_PAL={1:{a:"#fff3c4",b:"#f6c65a",c:"#b8801b",star:"#fff8dc"},2:{a:"#ffffff",b:"#d5dde6",c:"#8d9aa8",star:"#ffffff"},3:{a:"#ffe0c2",b:"#d9924f",c:"#96551f",star:"#fff1e2"}};
function medal(r){
  if(!r || r>3) return r;
  var p=_MDL_PAL[r], id="mmg"+(_MDL_ID++);
  return '<svg class="mdl" viewBox="0 0 24 24" width="100%" height="100%" aria-hidden="true">'
   +'<defs><linearGradient id="'+id+'" x1="0.2" y1="0" x2="0.8" y2="1">'
   +'<stop offset="0" stop-color="'+p.a+'"/><stop offset="0.45" stop-color="'+p.b+'"/>'
   +'<stop offset="1" stop-color="'+p.c+'"/></linearGradient></defs>'
   +'<polygon points="8.2,1.4 11.2,1.4 12,7.4 9,8.6" fill="#e05c5c"/>'
   +'<polygon points="15.8,1.4 12.8,1.4 12,7.4 15,8.6" fill="#2f6fb5"/>'
   +'<circle cx="12" cy="14.2" r="8.4" fill="url(#'+id+')"/>'
   +'<circle cx="12" cy="14.2" r="6.2" fill="none" stroke="rgba(255,255,255,.62)" stroke-width="1.1"/>'
   +'<path d="M12 10.1l1.25 2.55 2.8.4-2.03 1.98.48 2.79L12 16.5l-2.5 1.32.48-2.79-2.03-1.98 2.8-.4z" fill="'+p.star+'"/>'
   +'<path d="M6.6 9.4a7.2 7.2 0 0 1 6.2-2.6" stroke="rgba(255,255,255,.75)" stroke-width="1.2" fill="none" stroke-linecap="round"/>'
   +'</svg>';
}
/* global init-data helper */
function tk(){ try{ return window.__INIT || new URLSearchParams(location.search).get("initData") || ""; }catch(e){ return window.__INIT||""; } }
(function(){
var $=function(s){return document.getElementById(s)};
var D={name:"kaixin",avatar:"",level:"a",days:15,expire:"2026-11-10",created:"2026-08-15",
state:"正常",credit:156,unit:"茶叶",watch30:42.5,watchTotal:43.5,conc:{now:1,limit:4},
playing:{t:"沙丘2",s:"电影 · iPhone · Infuse",p:43},
recent:[{t:"沙丘2",p:43},{t:"繁花",p:72},{t:"奥本海默",p:18},{t:"三体",p:95},{t:"狂飙",p:11},{t:"走进科学",p:60}],
tg:"@JingCaiDiJia",admin:false,
server:{state:"在线",up:"12 天",live:{now:12,max:60}},
admin_ok:false};

/* ===== 真实数据：Telegram initData -> /app/api/profile ===== */
(function(){
  var P=window.__PROFILE;
  if(!P||!P.ok) return;
  var me=P.me||{};
  if(me.name){ D.name=me.name; D.embyName=me.name; }
  var TG=window.__TGUSER||{};
  var TGN=[TG.first_name,TG.last_name].filter(Boolean).join(" ").trim();
  /* 用户卡片只显示 Telegram 昵称，不再回退 username / Emby 用户名 */
  if(TGN) D.name=TGN;
  else if(me.tg_name) D.name=me.tg_name;
  D.avatar=me.photo||TG.photo_url||"";
  D.level=(me.level_key==="vip")?"a":(me.level_key==="bad"?"c":"b");
  if(me.currency) D.unit=me.currency;
  D.days=Math.max(0, (me.days_left==null?0:me.days_left));
  D.expire=me.expire||"—";
  if(me.created&&me.created!=="—") D.created=me.created;
  D.credit=(me.credit==null?0:me.credit);
  D.embyUser=me.name||"";
  D.embyPwd=me.emby_pwd||"";
  D.tg=me.username?("@"+me.username):(TG.username?("@"+TG.username):(me.tg_name||""));
  D.admin=!!P.admin;
  D.codeChanged=!!P.code_changed;
  D.state=(me.level_key==="bad")?"停用":"正常";
  D.online=P.online||0;
  D.devices=P.devices||[];
  D.conc={now:D.online, limit:(D.level==="a"?4:1)};
  D.watch30=(P.watch30==null?null:P.watch30);
  D.watchTotal=(P.watch_total==null?null:P.watch_total);
  D.lvOverride=(P.lv_override==null?0:P.lv_override);
  var pl=(P.playing||[])[0];
  D.playing=pl?{t:pl.title,s:[pl.type,pl.device,pl.client].filter(Boolean).join(" · "),p:pl.percent||0}:null;
  D.recent=(P.recent||[]).map(function(r){return {t:r.title,p:null,sub:r.played||r.sub||""};});
  D.server={state:"在线",up:"",live:{now:(P.live&&P.live.now!=null)?P.live.now:0,max:0}};
  window.__SERVER=P;
})();

// 等级 → 权益
var RIGHTS={
  a:{name:"尊贵白名单",tag:"永久有效",conc:4,desc:"4 路同播 · 优先响应 · 专属线路"},
  b:{name:"普通用户",tag:"保号中",conc:1,desc:"30 天内观看 · 仅限 1 个同播"}
};
if(window.FORCE_LEVEL) D.level=window.FORCE_LEVEL;

function right(key){ return RIGHTS[key]||RIGHTS.b; }

/* 头像：有图就加载（加载失败自动回退首字），没图直接显示首字 */
function avaFill(el,src,name){
  if(!el)return;
  var ch=String(name||"k").charAt(0).toUpperCase();
  el.textContent=ch;
  if(!src)return;
  var img=new Image();
  img.onload=function(){ el.innerHTML=""; el.appendChild(img); };
  img.onerror=function(){};
  img.alt=""; img.src=src;
}

/* ===== 使用规范（UI 渲染，替代原图片） ===== */
var RULES=[
 {t:"账号规则",items:[
   "每 30 天至少有效观影一次，逾期清理账号。",
   "每个账号仅限 1 台设备同时在线。",
   "禁止账号共享、转售、买卖及泄露内部线路。",
   "禁止异常下载、批量抓取及恶意请求。"]},
 {t:"客户端限制",items:[
   "暂不支持网易爆米花、Emby 官方客户端（含 Web）。",
   "魔改客户端、Infuse、VidHub、自定义 UA 等。",
   "受限客户端暂不提供技术支持。"]},
 {t:"网络说明",items:[
   "网络体验因地区及运营商而异，不保证直连。",
   "访问异常请尝试切换网络或使用推荐线路。"]},
 {t:"违规处理",items:[
   "视情节采取警告、限制功能或封禁账号等措施。",
   "严重违规可直接处理。"]}
];
function rulesHtml(){
  return RULES.map(function(g){
    return '<div class="ruItem"><div class="ruHd"><i></i><b>'+g.t+'</b></div><ul>'+
      g.items.map(function(x){return '<li>'+x+'</li>';}).join('')+'</ul></div>';
  }).join('');
}
if($("rulesHome"))$("rulesHome").innerHTML=rulesHtml();
if($("rulesFull"))$("rulesFull").innerHTML=rulesHtml();

if($("uName")){
  var RT=right(D.level);
  $("uName").textContent=D.name;
  if($("uLevel")){
    $("uLevel").textContent=RT.name;
    if($("tierBar"))$("tierBar").className="bigcard "+(D.level==="a"?"a":"b");
  }
  avaFill($("uAva"), D.avatar, D.name);
  if($("uWatch")){
    var _wh=[1,4,10,25,50,100,200], _wt=D.watchTotal||0, _wl=1;
    for(var _wi=0;_wi<_wh.length;_wi++){ if(_wt>=_wh[_wi]) _wl=_wi+1; else break; }
    var _ov=Number(D.lvOverride||0); if(_ov>=1&&_ov<=7) _wl=_ov;
    $("uWatch").textContent="LV"+_wl;
    $("uWatch").setAttribute("data-lv",_wl);
    if($("uMedal")){
      $("uMedal").innerHTML=medalImg(_wl);
      $("uMedal").setAttribute("data-lv",_wl);
      $("uMedal").setAttribute("title",(["初尝茶香","品茗入境","匠心调茶","茶韵名家","茶道宗师","百道传奇","巅峰至尊"][_wl-1]||""));
    }
  }

  if($("uDays"))$("uDays").textContent=D.days;
  if($("uDays2"))$("uDays2").textContent=D.days;
  if($("sCredit"))$("sCredit").textContent=D.credit;
  if($("sWatch"))$("sWatch").innerHTML=(D.watch30==null?"—":D.watch30+'<i>h</i>');
  /* 环与数字同源，避免出现「0h + 71%」这种不一致 */
  if($("rgCredit"))$("rgCredit").style.setProperty("--p",Math.min(100,Math.max(0,Number(D.credit||0)/10)));
  if($("rgWatch"))$("rgWatch").style.setProperty("--p",Math.min(100,Math.max(0,Number(D.watch30||0)*100/30)));
  if($("uExp"))$("uExp").textContent=D.expire;
  if($("uCr"))$("uCr").textContent=D.created;

  // 权益条
  if($("rightName"))$("rightName").textContent=RT.name;
  if($("rightTag"))$("rightTag").textContent=RT.tag;
  if($("rightDesc"))$("rightDesc").textContent=(D.level==="a"&&RT.tag?RT.tag+" · ":"")+RT.desc;
  if($("lvIco")) $("lvIco").src = "img/lv/"+(D.level==="a"?"vip":"normal")+".png?v=5";
  var rb=document.querySelector(".rights");
  if(rb)rb.classList.add(RT.cls);

  // 账户状态
  if($("uState")){
    var off=D.state==="停用";
    $("uState").textContent=off?"停用":"正常";
    $("uState").className=off?"bad":"ok";
  }
  if($("uRight"))$("uRight").textContent=RT.name;

  // 并发播放（EmbyBoss 限制）
  /* 并发路数先按等级写上限（真实在播数由 api/playing 静默刷新接管） */
  if($("sConc")&&D.conc){
    var lim=D.conc.limit||RT.conc||1;
    $("sConc").innerHTML='0<i>/'+lim+'</i>';
    if($("rgConc")) $("rgConc").style.setProperty("--p",0);
    if($("concLbl"))$("concLbl").textContent="正在播放";
  }

  var C=2*Math.PI*42, rv=$("ringVal");
  if(rv){rv.setAttribute("stroke-dasharray",C);rv.setAttribute("stroke-dashoffset",C*(1-Math.min(1,D.days/30)));}
  if($("pCount"))$("pCount").textContent=(D.conc&&D.conc.now?D.conc.now:0)+" 个会话";
  if($("playing"))$("playing").innerHTML=D.playing
    ? '<div class="rki"><div class="ava">▶</div><div><div class="nm">'+D.playing.t+'</div><div class="sub">'+D.playing.s+'</div><div class="pbar"><i style="width:'+D.playing.p+'%"></i></div></div><div class="val">'+D.playing.p+'%</div></div>'
    : '<div class="rki empty"><div class="sub">当前没有播放</div></div>';
  if($("recent")){
    var _rc=(D.recent||[]).slice(0,9), _pages=[], _i;
    for(_i=0;_i<_rc.length;_i+=3) _pages.push(_rc.slice(_i,_i+3));
    var _box=$("recent");
    if(!_rc.length){
      _box.className="posters"; _box.innerHTML='<div class="rcEmpty">还没有播放记录</div>';
    } else {
      _box.className="posters rcPager";
      _box.innerHTML=_pages.map(function(pg,pi){
        return '<div class="rcPg">'+pg.map(function(o){
          return '<div class="rc"><span class="t">'+o.t+'</span>'+
            (o.p==null?'':('<span class="bar"><i style="width:'+o.p+'%"></i></span>'))+'</div>';}).join("")+'</div>';
      }).join("");
      _box.scrollLeft=0;
    }
    var _rcHd=_box.parentNode.querySelector(".cnt");
    if(_rcHd){
      _rcHd.style.display=_rc.length?"":"none";
      _rcHd.className="cnt rcPageNo";
      _rcHd.textContent=_pages.length>1?("1/"+_pages.length):"";
      _rcHd.onclick=function(){
        if(_pages.length<2) return;
        var cur=Math.round(_box.scrollLeft/_box.clientWidth);
        var nxt=(cur+1)%_pages.length;
        _box.scrollTo({left:nxt*_box.clientWidth,behavior:"smooth"});
      };
      _box.onscroll=function(){
        if(_pages.length<2) return;
        var p=Math.round(_box.scrollLeft/_box.clientWidth)+1;
        _rcHd.textContent=Math.min(p,_pages.length)+"/"+_pages.length;
      };
    }
  }
}

/* ================= 排行榜 ================= */
var RKF=[
  {n:"空枪",en:"",m:207,c:2,h1:"#3b5b7a",h2:"#6f9ec4"},
  {n:"大唐妖探",en:"",m:81,c:2,h1:"#4a3a6b",h2:"#8f7ac0"},
  {n:"不择手段",en:"",m:77,c:1,h1:"#6b3a3a",h2:"#c07a6f"},
  {n:"腓尼基计划",en:"THE PHOENICIAN",m:24,c:2,h1:"#7a4a2a",h2:"#d1a06a"},
  {n:"城主之城",en:"",m:9,c:3,h1:"#2f5a4a",h2:"#6fb79c"},
  {n:"小羊肖恩",en:"美好时光",m:2,c:1,h1:"#3a4f6b",h2:"#7fa6cc"}
];
var SPAN={today:{k:1,label:"今日"},yest:{k:0.86,label:"昨日"},week:{k:6.3,label:"近7天"}};
var board="watch", span="today";

function fmtDur(m){
  m=Math.max(1,Math.round(m));
  var h=Math.floor(m/60), r=m%60;
  return h ? (h+"小时"+(r?r+"分":"")) : (m+"分钟");
}
function lvTag(lv){return '<i class="lvtag lv'+(lv>=7?"8":lv>=4?"3":"1")+'">LV'+lv+'</i>';}
function initials(n){
  var t=String(n).replace(/[^\u4e00-\u9fa5A-Za-z0-9]/g,"");
  return (t.slice(0,1)||"?").toUpperCase();
}
var __RK={}, rkBusy={}, RK_SEQ=0;
function rkKey(){return board+"|"+(board==="point"?"all":span);}
function rkList(){
  var hit=__RK[rkKey()];
  return hit ? hit.items : null;
}
/* 骨架占位：真实数据到达前只显示灰条，绝不显示假名字（旧版会先画一版演示数据，导致卡顿 + 死数据） */
function rkPodSkeleton(){
  return [[2,1],[1,0],[3,2]].map(function(p){
    return '<div class="podc p'+p[0]+' sk">'+
      '<div class="pcrown"></div>'+
      '<div class="pav skr skPav"></div>'+
      '<div class="pnm"><i class="skr skTxt" style="width:64%"></i></div>'+
      '<div class="pval"><i class="skr skTxt" style="width:46%"></i></div>'+
      '</div>';
  }).join("");
}
function rkRowSkeleton(n){
  var out="";
  for(var i=0;i<n;i++){
    out+='<div class="rkr sk"><span class="rkno">'+(i+4)+'</span>'+
      '<span class="rkava skr"></span>'+
      '<span class="rkname"><i class="skr skTxt" style="width:'+(52+((i*7)%26))+'%"></i></span>'+
      '<span class="rkval"><i class="skr skTxt" style="width:'+(34+(i%3)*10)+'%"></i></span></div>';
  }
  return out;
}
function rkAva(o,cls){
  return '<span class="'+cls+'">'+(o.av?'<img src="'+o.av+'" alt="" loading="lazy" decoding="async" onerror="this.remove()">':'')+initials(o.n)+'</span>';
}
/* 仅 minis:// 预览用：window.__MOCK_RANK 存在时才生效，线上永不触发 */
var RK_DEMO=[["Witness",229,2],["Mao Black",206,1],["库吃库吃吃",205,2],["Jason",172,2],["Joey Wong",167,1],
  ["东方武林",158,1],["sooyaaabo",101,2],["X XX",95,8],["明明明月",84,8],["Zen S",84,8],["Wp Xiao",973,1],
  ["Bob Chen",821,2],["张五",680,2],["Alex",612,3],["Nova",588,1]];
function rkFetch(){
  var k=rkKey(); if(__RK[k]||rkBusy[k]) return;
  if(window.__MOCK_RANK){
    __RK[k]={items:RK_DEMO.map(function(a){return {n:a[0],v:board==="watch"?a[1]*60:a[1],lv:a[2],av:""};}),
             me:{rk:48,v:board==="watch"?12:156}};
    renderRank(1); return;
  }
  rkBusy[k]=1;
  var seq=++RK_SEQ;
  fetch("api/rank?board="+board+"&span="+span,{cache:"no-store",headers:{"X-Tg-Init-Data":(window.__INIT||"")}})
    .then(function(r){return r.json();})
    .then(function(j){
      delete rkBusy[k];
      if(seq!==RK_SEQ) return;
      if(j&&j.ok&&j.items&&j.items.length){ __RK[k]={items:j.items,me:j.me||null}; }
      else { __RK[k]={items:[],me:null}; }
      renderRank(1);
    })["catch"](function(){ delete rkBusy[k]; });
}

function renderPodium(){
  var box=$("podium"); if(!box)return;
  box.hidden=false;
  var list=rkList();
  if(!list){ box.innerHTML=rkPodSkeleton(); return; }
  var top=list.slice(0,3);
  var order=[1,0,2];
  box.innerHTML=order.map(function(idx){
    var o=top[idx]; if(!o)return "";
    var rank=idx+1, cls="podc p"+rank;
    var val=board==="watch"?fmtDur(o.v):o.v+"<i>奶茶</i>";
    return '<div class="'+cls+'">'+
      '<div class="pcrown medal">'+medal(rank)+'</div>'+
      '<div class="pav">'+(o.av?'<img src="'+o.av+'" alt="" loading="lazy" decoding="async" onerror="this.remove()">':'')+initials(o.n)+'</div>'+
      '<div class="pnm">'+o.n+'</div>'+
      '<div class="plv">'+lvTag(o.lv)+'</div>'+
      '<div class="pval">'+val+'</div>'+
      '</div>';
  }).join("");
}
function renderList(){
  var box=$("rankList"); if(!box)return;
  var list=rkList();
  if(!list){ box.innerHTML=rkRowSkeleton(8); return; }
  if(!list.length){ box.innerHTML='<div class="rkEmpty">榜单暂无数据</div>'; return; }
  box.innerHTML=list.slice(3).map(function(o,i){
    var rank=i+4;
    var val=board==="watch"?fmtDur(o.v):'<b>'+o.v+'</b><i>奶茶</i>';
    return '<div class="rkr"><span class="rkno'+(rank<4?' medal':'')+'">'+medal(rank)+'</span>'+
      rkAva(o,"rkava")+
      '<span class="rkname">'+o.n+'</span>'+lvTag(o.lv)+
      '<span class="rkval">'+val+'</span></div>';
  }).join("");
}
/* ===== 热播榜（Emby 播放记录） ===== */
var __HOT={}, hotBusy={}, HOT_SEQ=0;
function hkEsc(t){
  return String(t==null?"":t).replace(/[&<>"]/g,function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];
  });
}
function hotFetch(){
  var k=span; if(__HOT[k]||hotBusy[k]) return;
  hotBusy[k]=1; var seq=++HOT_SEQ;
  fetch("api/hot?span="+encodeURIComponent(span)+"&limit=30",{cache:"no-store",
    headers:{"X-Tg-Init-Data":(window.__INIT||"")}})
    .then(function(r){return r.json()})
    .then(function(d){
      hotBusy[k]=0;
      if(!d||!d.ok) return;
      __HOT[k]=d.items||[];
      if(seq===HOT_SEQ&&board==="hot") renderHot();
    })
    .catch(function(){hotBusy[k]=0});
}
function renderHot(){
  var box=$("rankList"); if(!box) return;
  var list=__HOT[span];
  if(!list){
    var sk="";
    for(var i=0;i<8;i++) sk+='<div class="rkr sk"><span class="rkno">'+(i+1)+
      '</span><span class="rkname skr"></span><span class="rkval skr"></span></div>';
    box.innerHTML=sk; return;
  }
  if(!list.length){ box.innerHTML='<div class="rkEmpty">暂无播放数据</div>'; return; }
  box.innerHTML=list.slice(0,30).map(function(o,i){
    return '<div class="rkr hk'+(i<3?" hr3":"")+'" data-hn="'+hkEsc(o.n)+'" data-hc="'+(o.c||0)+
      '" data-hl="'+SPAN[span].label+'">'+
      '<span class="rkno'+(i<3?' medal':'')+'">'+medal(i+1)+'</span>'+
      '<span class="rkname">'+hkEsc(o.n)+'</span>'+
      '<span class="rkval"><b>'+o.c+'</b><i>次</i></span>'+
      '<i class="hkChev">\u203a</i></div>';
  }).join("");
}
function renderRank(noFetch){
  if(board==="hot"){
    if($("rankTitle")) $("rankTitle").textContent="热播榜";
    if($("rankSub")) $("rankSub").textContent=SPAN[span].label+" \u00b7 TOP 30";
    var hc=$("rankChips"), hs=hc&&hc.parentNode;
    if(hc) hc.style.display="";
    if(hs) hs.style.display="";
    var pd=$("podium"); if(pd){ pd.hidden=true; pd.innerHTML=""; }
    var mb=$("meBar"); if(mb) mb.hidden=true;
    renderHot(); if(!noFetch) hotFetch();
    return;
  }
  if($("rankTitle")) $("rankTitle").textContent=board==="watch"?"观影榜":"奶茶榜";
  if($("rankSub")) $("rankSub").textContent=(board==="watch"?SPAN[span].label:"累计")+" · TOP 30";
  var _ch=$("rankChips"), _sub=_ch&&_ch.parentNode;
  if(_ch) _ch.style.display=(board==="watch")?"":"none";
  if(_sub) _sub.style.display=(board==="watch")?"":"none";
  renderPodium(); renderList();
  var bar=$("meBar"), hit=__RK[rkKey()], me=hit&&hit.me, loaded=!!hit;
  if(bar){
    if($("meRk")) $("meRk").textContent=loaded?(me?("#"+me.rk):"—"):"—";
    if($("meVal")) $("meVal").textContent=loaded?(me?(board==="watch"?fmtDur(me.v):me.v+" 奶茶"):"未上榜"):"加载中…";
    bar.hidden=false;
  }
  if(!noFetch) rkFetch();
}
/* 四条服务器域名：[标签, 名称, 域名, 是否尊享] —— 尊享条目仅 level==="a"（白名单）可见 */
var DOMAINS=[
  ["主","普通域名","emby.example.com",0],
  ["备","普通备用","emby2.example.com",0],
  ["尊","尊享域名","white.example.com",1],
  ["尊备","尊享备用","white2.example.com",1]
];
var COPY_ICO='<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2.6"/><path d="M6.2 15H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h7a2 2 0 0 1 2 2v.2"/></svg>';
var CHK_ICO='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4.6 12.6l4.9 4.9L19.4 7"/></svg>';
var ERR_ICO='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';
var PING_MOCK=/^(minis|file|blob|about):/.test(location.protocol)&&!window.__PING_REAL;
var PING_SEED={"emby.example.com":36,"emby2.example.com":88,"white.example.com":26,"white2.example.com":54};

function domDots(a){ return "\u2022\u2022\u2022\u2022\u2022\u2022"; }
function renderDomains(){
  var box=document.getElementById("domList"); if(!box)return;
  var vip=(D.level==="a");
  var EYE_ICO='<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12z"/><circle cx="12" cy="12" r="2.6"/></svg>';
  var list=DOMAINS.filter(function(d){return !d[3]||vip;});
  box.innerHTML=list.map(function(d){
    return '<div class="dm'+(d[3]?" vip":"")+'" data-copy="'+d[2]+'" data-host="'+d[2]+'">'+
      '<div class="dmG"><div class="dmT">'+d[1]+(d[3]?'<b class="dmBadge">白名单</b>':'')+
      '</div><div class="dmV mask" data-addr="'+d[2]+'">'+domDots(d[2])+'</div></div>'+
      '<span class="ping">--</span>'+
      '<button class="copyBtn sm hBtn" type="button" aria-label="显示 '+d[1]+'"><span>显示</span></button>'+
    '</div>';
  }).join("");
  var tip=document.getElementById("domTip");
  if(tip) tip.textContent="任选一条填入客户端，连不上就换下一条。延迟由你的网络实时测得，点数字可重测。"+(vip?"尊享域名仅限白名单用户使用。":"");
  pingAll();
}

/* ===== 延迟测速 =====
   原理：fetch(no-cors) 计时 = DNS + TCP + TLS + HTTP 首字节的完整往返（读不到响应体，但链路是真的）
   优化点：
   1) 打到真实接口 /emby/System/Info/Public（用户实际访问的路径），失败再回落站点根路径
   2) 第一次采样含 DNS/TLS 握手，偏大，直接丢弃
   3) 余下采样取中位数（比最小值稳定，不会被偶发幸运包带偏），并记录抖动
   4) 单次 4s 超时 + AbortController，避免卡住整轮
*/
var PING_PATHS=["/emby/System/Info/Public","/"];
var PING_N=4;
function pingOnce(host,path){
  return new Promise(function(res){
    var t0=(window.performance&&performance.now)?performance.now():Date.now();
    var done=false, ctl=null;
    if(typeof AbortController!=="undefined") ctl=new AbortController();
    var to=setTimeout(function(){
      if(done) return; done=true;
      if(ctl&&ctl.abort){ try{ctl.abort();}catch(e){} }
      res(-1);
    },4000);
    var opt={mode:"no-cors",cache:"no-store",credentials:"omit",redirect:"follow"};
    if(ctl) opt.signal=ctl.signal;
    var sep=path.indexOf("?")>=0?"&":"?";
    fetch("https://"+host+path+sep+"_p="+Date.now()+Math.floor(Math.random()*900),opt).then(function(){
      if(done) return; done=true; clearTimeout(to);
      res(Math.round(((window.performance&&performance.now)?performance.now():Date.now())-t0));
    },function(){
      if(done) return; done=true; clearTimeout(to); res(-1);
    });
  });
}
function pingSeq(host,path,n){
  var got=[];
  return new Promise(function(done){
    (function round(i){
      if(i>=n){ done(got); return; }
      pingOnce(host,path).then(function(ms){
        if(ms>=0) got.push(ms);
        setTimeout(function(){ round(i+1); }, i?90:0);
      });
    })(0);
  });
}
function pingHost(host){
  if(PING_MOCK){
    var b=PING_SEED[host]||45;
    return new Promise(function(r){
      setTimeout(function(){ r({ms:b+Math.round(Math.random()*8-4),jit:Math.round(Math.random()*6)}); }, 260+Math.random()*420);
    });
  }
  return pingSeq(host,PING_PATHS[0],PING_N).then(function(g){
    if(g.length>=2) return g;
    return pingSeq(host,PING_PATHS[1],3);
  }).then(function(g){
    if(!g.length) return {ms:-1,jit:0};
    var s=g.slice(1); if(!s.length) s=g.slice();
    s.sort(function(a,b){ return a-b; });
    var med=s[Math.floor(s.length/2)];
    return {ms:med,jit:s.length>1?(s[s.length-1]-s[0]):0};
  });
}
function paintPing(row,r){
  var p=row.querySelector(".ping"); if(!p) return;
  var ms=(r&&typeof r==="object")?r.ms:r;
  var jit=(r&&typeof r==="object")?r.jit:0;
  if(ms<0){ p.className="ping bad"; p.textContent="超时"; p.title="4 秒内无响应"; }
  else{
    p.className="ping"+(ms<120?"":(ms<320?" mid":" bad"));
    p.textContent=ms+" ms";
    p.title="实测 "+ms+" ms"+(jit?" · 抖动 ±"+jit+" ms":"")+"（真实 TCP+TLS+HTTP 往返，取中位数）";
  }
}
function pingAll(){
  var rows=[].slice.call(document.querySelectorAll("#domList .dm"));
  if(!rows.length) return;
  var btn=document.getElementById("btnSpeed");
  if(btn) btn.classList.add("busy");
  rows.forEach(function(row){
    var p=row.querySelector(".ping");
    if(p){ p.className="ping tst"; p.textContent="测速"; p.title="正在测量…"; }
  });
  Promise.all(rows.map(function(row){
    return pingHost(row.getAttribute("data-host")).then(function(r){ paintPing(row,r); });
  })).then(function(){ if(btn) btn.classList.remove("busy"); });
}

/* ===== 域名复制（点整行或点按钮都可以） ===== */
(function(){
  function legacyCopy(t,cb){
    var ta=document.createElement("textarea");
    ta.value=t; ta.setAttribute("readonly","");
    ta.style.cssText="position:fixed;left:-9999px;top:0;opacity:0";
    document.body.appendChild(ta);
    ta.select();
    if(ta.setSelectionRange) ta.setSelectionRange(0,t.length);
    var ok=false;
    try{ ok=document.execCommand("copy"); }catch(e){ ok=false; }
    document.body.removeChild(ta);
    cb(ok);
  }
  function doCopy(btn,t,cb){
    function done(k){ if(cb) cb(k); }
    if(navigator.clipboard&&navigator.clipboard.writeText){
      navigator.clipboard.writeText(t).then(function(){done(true);},function(){ legacyCopy(t,done); });
    } else legacyCopy(t,done);
  }
  function paintRow(row,on){
    var v=row.querySelector(".dmV"); if(!v) return;
    var a=v.getAttribute("data-addr")||"";
    if(on){ v.textContent=a; v.classList.remove("mask"); }
    else { v.textContent=domDots(a); v.classList.add("mask"); }
    row.setAttribute("data-shown",on?"1":"0");
  }
  function setStep(row,n,copied){
    row.setAttribute("data-step",String(n));
    var b=row.querySelector(".hBtn"), sp=b&&b.querySelector("span");
    if(b){ b.classList.remove("ok","err"); }
    if(n===0){ paintRow(row,false); if(sp) sp.textContent="显示"; }
    else if(n===1){ paintRow(row,true); if(sp) sp.textContent="复制"; }
    else {
      if(sp) sp.textContent=copied?"已复制":"收起";
      if(copied&&b){
        b.classList.add("ok"); clearTimeout(b._t);
        b._t=setTimeout(function(){
          if(row.getAttribute("data-step")==="2"&&sp){ sp.textContent="收起"; b.classList.remove("ok"); }
        },1300);
      }
    }
  }
  function advance(row){
    var b=row.querySelector(".hBtn");
    var st=parseInt(row.getAttribute("data-step")||"0",10);
    if(st===0){ setStep(row,1); return; }
    if(st===1){
      setStep(row,2,false);
      doCopy(b,row.getAttribute("data-copy")||"",function(ok){
        setStep(row,2,ok);
        if(!ok&&b){
          b.classList.add("err");
          var sp2=b.querySelector("span"); if(sp2) sp2.textContent="复制失败";
          setTimeout(function(){ if(sp2) sp2.textContent="收起"; b.classList.remove("err"); },1500);
        }
      });
      return;
    }
    setStep(row,0);
  }
  document.addEventListener("click",function(e){
    if(!e.target.closest) return;
    if(e.target.closest("#btnSpeed")){ pingAll(); return; }
    if(e.target.closest("#domList .ping")){
      var r0=e.target.closest(".dm"); if(r0) pingAll();
      return;
    }
    var row=e.target.closest(".dm");
    if(!row) return;
    e.preventDefault();
    advance(row);
  });
  document.addEventListener("click",function(e){
    var t=e.target.closest?e.target.closest(".tabbar .tab"):null;
    if(t&&t.getAttribute("data-pg")==="pgLine") setTimeout(pingAll,280);
  });
})();
/* ===== 签到（/app/api/checkin，算法与 EmbyBoss 机器人一致）===== */
var SIGN={signed:false,streak:0,days:{},credit:null,unit:"茶叶",today:"",today_pts:0,on:true};
function signApi(method){
  return fetch("api/checkin",{method:method,cache:"no-store",
    headers:{"X-Tg-Init-Data":window.__INIT||""}}).then(function(r){return r.json();});
}
function pad2(n){return ("0"+n).slice(-2);}
function renderCal(){
  var box=document.getElementById("cal"); if(!box)return;
  var d0=SIGN.today?new Date(SIGN.today.replace(/-/g,"/")):new Date();
  var y=d0.getFullYear(),m=d0.getMonth(),td=d0.getDate();
  var total=new Date(y,m+1,0).getDate(),first=new Date(y,m,1).getDay();
  var out="",cnt=0;
  for(var i=0;i<first;i++) out+='<i class="void"></i>';
  for(var d=1;d<=total;d++){
    var key=y+"-"+pad2(m+1)+"-"+pad2(d);
    var has=Object.prototype.hasOwnProperty.call(SIGN.days,key);
    if(has||(key===SIGN.today&&SIGN.signed)){cnt++;out+='<i class="done'+(d===td?" today":"")+'">'+d+'</i>';}
    else out+='<i class="'+(d===td?"today":"")+'">'+d+'</i>';
  }
  box.innerHTML=out;
  var sc=document.getElementById("signCount");
  if(sc) sc.textContent=cnt?(cnt+" 天"):"暂无记录";
}
function renderSign(){
  var big=document.getElementById("signStreak"); if(big) big.textContent=SIGN.streak||0;
  var b=document.getElementById("btnSign");
  if(b){
    if(!SIGN.on){ b.textContent="签到未开启"; b.disabled=true; b.className="btn primary off"; }
    else if(SIGN.signed){
      b.textContent="今日已签到"+(SIGN.today_pts?" · +"+SIGN.today_pts+" "+SIGN.unit:"");
      b.disabled=true; b.className="btn primary off";
    }else{
      b.textContent="今日签到"; b.disabled=false; b.className="btn primary";
    }
  }
  if(SIGN.credit!=null){ var bn=document.getElementById("balNum"); if(bn) bn.textContent=SIGN.credit; }
  if(window.__SHOP__&&__SHOP__.sync) __SHOP__.sync();
  renderCal();
}
function signApply(d){
  SIGN.signed=!!d.signed; SIGN.streak=d.streak||0; SIGN.days=d.days||{};
  if(d.credit!=null) SIGN.credit=d.credit;
  if(d.unit) SIGN.unit=d.unit;
  if(d.today) SIGN.today=d.today;
  if(d.today_pts!=null) SIGN.today_pts=d.today_pts;
}
function loadSign(){
  if(!window.__INIT) return;
  signApi("GET").then(function(d){
    if(!d||!d.ok){ if(d&&d.error==="closed"){SIGN.on=false;renderSign();} return; }
    signApply(d); renderSign();
  }).catch(function(){});
}
document.addEventListener("click",function(e){
  var b=e.target.closest?e.target.closest("#btnSign"):null;
  if(!b||b.disabled) return;
  b.disabled=true; b.textContent="签到中…";
  signApi("POST").then(function(d){
    if(d&&d.ok){
      signApply(d); SIGN.today_pts=d.reward||0;
      setToast("签到成功 +"+d.reward+" "+(d.unit||"茶叶"));
      renderSign();
    }else if(d&&d.error==="signed"){
      signApply(d); setToast("今天已经签到过了"); renderSign();
    }else if(d&&d.error==="forbidden"){
      setToast("当前等级暂无签到权限"); b.disabled=false; b.textContent="今日签到";
    }else if(d&&d.error==="closed"){
      SIGN.on=false; renderSign(); setToast("签到功能未开启");
    }else{
      setToast("签到失败，请稍后再试"); b.disabled=false; b.textContent="今日签到";
    }
  }).catch(function(){
    setToast("网络异常，请稍后再试"); b.disabled=false; b.textContent="今日签到";
  });
});

// ===== 观影勋章 =====
var TIERS={
  // 茶色系：[高光, 主色, 描边]
  bronze:["#eef9dd","#8ec548","#6b9c2c"],  // 初尝茶香 · 嫩芽绿
  silver:["#e6f5e9","#5aa873","#3f8355"],  // 品茗入境 · 抹茶绿
  gold:  ["#ffeccd","#dd9a48","#b3742b"],  // 匠心调茶 · 焙茶琥珀
  plat:  ["#fbefd6","#c49a52","#9d7735"],  // 茶韵名家 · 乌龙金褐
  dia:   ["#f9dedb","#ab4642","#88332f"],  // 茶道宗师 · 普洱深红
  star:  ["#ddecfa","#2f83c8","#1f6098"]   // 百道传奇 · 茶百道蓝
};
var MEDALS=[
  {i:1,name:"初尝茶香",h:1,up:1},
  {i:2,name:"品茗入境",h:4,up:3},
  {i:3,name:"匠心调茶",h:10,up:6},
  {i:4,name:"茶韵名家",h:25,up:15},
  {i:5,name:"茶道宗师",h:50,up:25},
  {i:6,name:"百道传奇",h:100,up:50},
  {i:7,name:"巅峰至尊",h:200,up:100}
];
var STAR="M0,-6.2 L1.82,-1.9 L6.4,-1.9 L2.7,1.02 L4.1,5.4 L0,2.72 L-4.1,5.4 L-2.7,1.02 L-6.4,-1.9 L-1.82,-1.9 Z";
var _gid=0;
function medalSvg(k,n,on){
  var c=TIERS[k]||TIERS.bronze, id="mg"+(++_gid);
  var stc=(k==="star")?"#ffe6a2":"#fff";
  var xs=n===1?[0]:n===2?[-4.8,4.8]:[-6.6,0,6.6];
  var sc=n===1?1:n===2?.66:.56;
  // 齿边（滚花）
  var ticks="";
  for(var i=0;i<36;i++){
    var a2=i*10*Math.PI/180;
    ticks+='<line x1="'+(20+Math.cos(a2)*15.4).toFixed(2)+'" y1="'+(20+Math.sin(a2)*15.4).toFixed(2)+
      '" x2="'+(20+Math.cos(a2)*16.8).toFixed(2)+'" y2="'+(20+Math.sin(a2)*16.8).toFixed(2)+
      '" stroke="rgba(60,30,0,.22)" stroke-width=".5"/>';
  }
  // 星：先压一层暗色模拟凹陷，再叠亮面
  var st=xs.map(function(x){
    return '<g transform="translate('+(20+x)+',21.1) scale('+sc+')">'+
        '<path d="'+STAR+'" fill="rgba(40,20,0,.32)"/></g>'+
      '<g transform="translate('+(20+x)+',20.6) scale('+sc+')">'+
        '<path d="'+STAR+'" fill="'+stc+'"/></g>';
  }).join("");
  return '<svg viewBox="0 0 40 40">'+
    '<defs>'+
      '<linearGradient id="'+id+'o" x1=".15" y1="0" x2=".85" y2="1">'+
        '<stop offset="0" stop-color="#ffffff" stop-opacity=".95"/>'+
        '<stop offset=".38" stop-color="'+c[0]+'"/>'+
        '<stop offset=".72" stop-color="'+c[1]+'"/>'+
        '<stop offset="1" stop-color="'+c[2]+'"/></linearGradient>'+
      '<radialGradient id="'+id+'f" cx=".33" cy=".24" r=".92">'+
        '<stop offset="0" stop-color="'+c[0]+'"/>'+
        '<stop offset=".5" stop-color="'+c[1]+'"/>'+
        '<stop offset="1" stop-color="'+c[2]+'"/></radialGradient>'+
      '<linearGradient id="'+id+'s" x1="0" y1="0" x2="0" y2="1">'+
        '<stop offset="0" stop-color="#fff" stop-opacity=".30"/>'+
        '<stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>'+
      '<radialGradient id="'+id+'d" cx=".34" cy=".26" r=".95">'+
        '<stop offset="0" stop-color="#5a3000" stop-opacity="0"/>'+
        '<stop offset=".58" stop-color="#5a3000" stop-opacity="0"/>'+
        '<stop offset="1" stop-color="#5a3000" stop-opacity=".34"/></radialGradient>'+
    '</defs>'+
    // 金属外圈（饱满环）
    '<circle cx="20" cy="20" r="19.2" fill="url(#'+id+'o)"/>'+
    '<circle cx="20" cy="20" r="17.9" fill="none" stroke="rgba(255,255,255,.55)" stroke-width=".6"/>'+
    // 盘面
    '<circle cx="20" cy="20" r="16.9" fill="url(#'+id+'f)"/>'+
    // 盘面下沿暗部（立体）
    '<circle cx="20" cy="20" r="16.9" fill="url(#'+id+'d)"/>'+
    ticks+
    // 内圈双重压边（凹槽感）
    '<circle cx="20" cy="20" r="16.9" fill="none" stroke="rgba(50,25,0,.22)" stroke-width=".9"/>'+
    '<circle cx="20" cy="20" r="15.7" fill="none" stroke="rgba(255,255,255,.40)" stroke-width=".7"/>'+
    // 高光斜带
    '<ellipse cx="14.2" cy="12.4" rx="8.8" ry="5.2" fill="url(#'+id+'s)" transform="rotate(-30 14.2 12.4)"/>'+
    // 底部反光
    '<ellipse cx="23" cy="30.4" rx="7.6" ry="2.4" fill="#fff" opacity=".10"/>'+
    st+'</svg>';
}
// ===== 七级勋章（观影等级）=====
var MEDALS_DESC=0;
function medalImg(i,cls){return '<img src="img/lv/lv'+i+'.png?v=5" alt=""'+(cls?' class="'+cls+'"':'')+'>';}
function curLevel(total){
  var cur=null;
  for(var i=0;i<MEDALS.length;i++){ if(total>=MEDALS[i].h) cur=MEDALS[i]; else break; }
  return cur;
}
function renderMedal(){
  var total=D.watchTotal||0, cur=curLevel(total);
  var _ov=Number(D.lvOverride||0); if(_ov>=1&&_ov<=7) cur=MEDALS[_ov-1];
  var ci=cur?cur.i-1:-1;
  var nxt=MEDALS[ci+1]||null;
  if($("mIco")) $("mIco").innerHTML = cur ? medalImg(cur.i) : "";
  if($("mName")) $("mName").textContent = cur ? cur.name : "新人观众";
  if($("mSub")){
    var t=fmtH(total);
    $("mSub").textContent = nxt
      ? "累计 "+t+" 小时 · 距 "+nxt.name+" 还差 "+fmtH(nxt.h-total)+" 小时"
      : "累计 "+t+" 小时 · 已解锁全部勋章";
  }
  if($("mBar")){
    var lo=cur?cur.h:0, hi=nxt?nxt.h:lo, pct=nxt?Math.max(4,Math.min(100,(total-lo)/(hi-lo)*100)):100;
    $("mBar").style.width=pct+"%";
  }
  var list=$("mList");
  if(list){
    list.innerHTML=MEDALS.map(function(m){
      var on=cur?m.i<=cur.i:(total>=m.h), isCur=cur&&cur.i===m.i;
      return '<div class="mg'+(on?"":" off")+(isCur?" cur":"")+'">'+
        medalImg(m.i)+'<b>'+m.name+'</b><span>'+(on?m.h+' 小时':"≥ "+m.h+" 小时")+'</span>'+
        '<u>'+(m.i===1?'':'+'+m.up+' 小时')+'</u></div>';
    }).join("");
  }
}
function fmtH(v){ return (Math.round(v*10)/10).toString().replace(/\.0$/,""); }
function openMedal(){
  var sh=$("medalSheet"); if(!sh)return;
  renderMedal(); sh.classList.add("on"); sh.setAttribute("aria-hidden","false");
  document.body.style.overflow="hidden";
}
function closeMedal(){
  var sh=$("medalSheet"); if(!sh)return;
  sh.classList.remove("on"); sh.setAttribute("aria-hidden","true");
  document.body.style.overflow="";
}

function go(id){
  document.querySelectorAll(".page").forEach(function(p){p.classList.toggle("on",p.id===id)});
  document.querySelectorAll(".tab").forEach(function(t){t.classList.toggle("on",t.dataset.pg===id)});
  window.scrollTo({top:0,behavior:"instant"});
}
document.querySelectorAll(".tab").forEach(function(t){
  t.addEventListener("click",function(e){e.preventDefault();go(t.dataset.pg);});
});
/* ===== 设置页（对齐 EmbyBoss 功能） ===== */
var IS_ADMIN=!!(window.__ADMIN||D.admin);
var LIVE=D.server&&D.server.live?D.server.live:{now:12,max:60};

var ICO={
 key:'<svg viewBox="0 0 24 24"><circle cx="8" cy="8" r="4.4"/><path d="M11.2 11.2 20 20M17 17l1.6-1.6M14.4 14.4l1.6-1.6"/></svg>',
 lock:'<svg viewBox="0 0 24 24"><rect x="4.6" y="10.2" width="14.8" height="10.2" rx="2.6"/><path d="M8.4 10.2V7.6a3.6 3.6 0 0 1 7.2 0v2.6"/><circle cx="12" cy="15.2" r="1.3"/></svg>',
 swap:'<svg viewBox="0 0 24 24"><path d="M4 8.4h13.2l-3-3M20 15.6H6.8l3 3"/></svg>',
 trash:'<svg viewBox="0 0 24 24"><path d="M4.6 7h14.8M9.4 7V5.2h5.2V7M7 7l.9 12.2a1.6 1.6 0 0 0 1.6 1.5h5a1.6 1.6 0 0 0 1.6-1.5L17 7M10.2 11v6M13.8 11v6"/></svg>',
 server:'<svg viewBox="0 0 24 24"><rect x="3.6" y="4.4" width="16.8" height="6" rx="2"/><rect x="3.6" y="13.6" width="16.8" height="6" rx="2"/><path d="M7 7.4h.01M7 16.6h.01"/></svg>',
 users:'<svg viewBox="0 0 24 24"><circle cx="9.2" cy="8.4" r="3.4"/><path d="M3.4 19.6a5.8 5.8 0 0 1 11.6 0"/><path d="M16 5.6a3.2 3.2 0 0 1 0 5.8M17.4 14.6a5.4 5.4 0 0 1 3.2 4M20.6"/></svg>',
 monitor:'<svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="12" rx="2.2"/><path d="M9 20.2h6M12 17.2v3"/></svg>',
 wifi:'<svg viewBox="0 0 24 24"><path d="M3.4 9.2a13 13 0 0 1 17.2 0M6.6 12.8a8.6 8.6 0 0 1 10.8 0M9.8 16.4a4.2 4.2 0 0 1 4.4 0"/><circle cx="12" cy="19.6" r="1"/></svg>',
 chat:'<svg viewBox="0 0 24 24"><path d="M20.4 12.4c0 4-3.8 7.2-8.4 7.2a10 10 0 0 1-2.8-.4L4.4 20.8l1.3-3.6A6.9 6.9 0 0 1 3.6 12.4c0-4 3.8-7.2 8.4-7.2s8.4 3.2 8.4 7.2z"/></svg>',
 doc:'<svg viewBox="0 0 24 24"><path d="M6 3.4h7.6L19 8.8v11.8H6z"/><path d="M13.4 3.6v5.4H19"/></svg>',
 info:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.6"/><path d="M12 10.8v5.4M12 7.9h.01"/></svg>',
 gear:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3.1"/><path d="M12 3.2v2.2M12 18.6v2.2M4.9 8l1.9 1.1M17.2 14.9l1.9 1.1M4.9 16l1.9-1.1M17.2 9.1l1.9-1.1"/></svg>',
 cart:'<svg viewBox="0 0 24 24"><path d="M3.4 4.6h2.4l2.3 10.2h9.2l2.1-7.4H6.6"/><circle cx="9.4" cy="18.6" r="1.4"/><circle cx="17" cy="18.6" r="1.4"/></svg>'
};

function srow(o){
  return '<div class="srow'+(o.danger?" danger":"")+'" data-act="'+o.act+'" role="button" tabindex="0">'+
    '<span class="sico'+(o.tone?" "+o.tone:"")+'">'+(ICO[o.ico]||"")+'</span>'+
    '<span class="stx"><span class="sname">'+o.name+'</span>'+
      (o.sub?'<span class="ssub">'+o.sub+'</span>':'')+'</span>'+
    (o.val?'<span class="sval">'+o.val+'</span>':'')+
    (o.go?'<span class="sgo">›</span>':'')+'</div>';
}
function scard(o){
  return '<section class="sec glass setCard'+(o.cls?" "+o.cls:"")+'">'+
    '<div class="setHd"><b>'+o.title+'</b>'+(o.badge?'<span class="setBadge">'+o.badge+'</span>':'')+
      (o.note?'<span class="setNote">'+o.note+'</span>':'')+'</div>'+
    o.rows.join("")+'</section>';
}

function renderSet(){
  var u=document.getElementById("setUser");
  if(u){
    var R=RIGHTS[D.level]||RIGHTS.b;
    u.innerHTML='<div class="suAva">'+(D.avatar?'<img src="'+D.avatar+'" alt="">':D.name.charAt(0).toUpperCase())+'</div>'+
      '<div class="suTx"><div class="suN">'+D.name+(IS_ADMIN?'<span class="suAdm">管理员</span>':'')+'</div>'+
      '<div class="suS">'+D.tg+' · '+R.name+(D.ctitle?' · '+D.ctitle:'')+'</div></div>'+
      '<div class="suR"><div class="suD">'+((D.level==="a")?'永久':D.expire)+'</div><div class="suL">'+((D.level==="a")?'有效':'到期')+'</div></div>';
  }
  var body=document.getElementById("setBody"); if(!body)return;
  if(!body.__lkBound){ body.__lkBound=1;
    body.addEventListener("click",function(e){
      var lk=e.target&&e.target.closest?e.target.closest(".setLock"):null;
      if(lk) setToast("需要 EmbyBoss 管理员权限，可联系管理员开通");
    });
  }

  var acct=scard({title:"账户操作",note:"EmbyBoss",rows:[
    srow({act:"pwd",ico:"key",name:"重置密码",sub:"清空后需自行设置新密码",go:1}),
    D.codeChanged
      ? srow({act:"code",ico:"lock",name:"修改安全码",val:'<span class="sdim">已修改</span>',sub:"每账号仅可修改一次"})
      : srow({act:"code",ico:"lock",name:"修改安全码",sub:"每账号仅可修改一次",go:1}),
    srow({act:"rebind",ico:"swap",name:"换绑 Telegram",sub:"提交后由管理员审核",go:1}),
    srow({act:"del",ico:"trash",name:"删除账号",sub:"永久删除，不可恢复",go:1,danger:1})
  ]});

  var emby=scard({title:"Emby 状态",rows:[
    srow({act:"st",ico:"server",name:"服务器状态",val:'<i class="sdot ok"></i>'+D.server.state}),
    srow({act:"live",ico:"users",name:"实时观看",val:'<b id="liveNum" class="liveN">'+LIVE.now+'</b><span class="sdim">'+(LIVE.max?(' / '+LIVE.max):' · 正在播放')+'</span>'}),
    srow({act:"dev",ico:"monitor",name:"我的设备",val:'<span class="sdim">'+((D.devices&&D.devices.length)||0)+' 台</span>',go:1}),
    srow({act:"line",ico:"wifi",name:"我的线路",val:'<span class="sdim">'+(D.level==="a"?"尊享域名":"普通域名")+'</span>',go:1})
  ]});

  var help=scard({title:"帮助与支持",rows:[
    srow({act:"contact",ico:"chat",name:"联系管理员",sub:"点击复制 @JingCaiDiJiaBot",go:1}),
    srow({act:"rules",ico:"doc",name:"使用规范",go:1}),
    srow({act:"about",ico:"info",name:"关于",val:'<span class="sdim">v2.0.0</span>',go:1})
  ]});

  var out=acct+emby+help;

  if(IS_ADMIN){
    out+=scard({title:"管理员设置",badge:"仅管理员可见",cls:"setAdmin",rows:[
      srow({act:"sys",ico:"gear",name:"系统设置",sub:"公告、客服、维护与运行状态",go:1,tone:"adm"}),
      srow({act:"shop",ico:"cart",name:"商城管理",sub:"上架商品、库存与价格",go:1,tone:"adm"})
    ]});
  }else{
    out+='<section class="sec glass setCard setLock"><div class="lkRow"><span class="lkIco">'+ICO.lock+'</span>'+
      '<span class="stx"><span class="sname">系统设置 · 商城管理</span>'+
      '<span class="ssub">需要 EmbyBoss 管理员权限</span></span></div></section>';
  }
  body.innerHTML=out;
}

/* ---- 实时观看人数（接 /api/live 时替换 tickLive 内部即可） ---- */
function tickLive(){
  var el=document.getElementById("liveNum"); if(!el)return;
  el.classList.add("tick");
  setTimeout(function(){el.classList.remove("tick");},460);
  fetch("api/live",{cache:"no-store",headers:{"X-Tg-Init-Data":window.__INIT||""}}).then(function(r){return r.json()}).then(function(d){
    if(d&&d.ok&&d.now!=null){LIVE.now=d.now; if(d.max)LIVE.max=d.max; el.textContent=LIVE.now;}
  }).catch(function(){});
}
if(!window.__LIVET){
  window.__LIVET=setInterval(function(){
    var pg=document.getElementById("pgSet");
    if(pg&&pg.classList.contains("on")) tickLive();
  },18000);
}
/* ---- 操作弹层 ---- */
var ACCT_ERR={
  bad_code:"安全码不能为空、不能含空格，最多 32 个字符",
  same_code:"新安全码和当前的一致，请换一个",
  already_set:"修改安全码的次数已用完，需要重置请联系管理员",
  bad_pwd2:"安全码不正确，请重试",
  no_code:"请先设置安全码，再进行此操作",
  bad_tg:"请输入新的 Telegram 数字 ID",
  same_tg:"与当前绑定的账号相同",
  tg_taken:"该 Telegram 已绑定其他账号",
  not_registered:"账号还没开通，请先在机器人里开通",
  save_failed:"保存失败，请稍后再试",
  reset_failed:"重置密码失败，请稍后再试",
  closed:"该功能暂未开放",
  unauthorized:"身份校验失败，请从 Telegram 重新打开",
  network:"网络异常，请检查网络后重试"
};
function acctPost(act,value){
  return fetch("api/account",{method:"POST",cache:"no-store",
    headers:{"Content-Type":"application/json","X-Tg-Init-Data":(window.__INIT||"")},
    body:JSON.stringify({act:act,value:value||""})})
    .then(function(r){ return r.json(); })
    .catch(function(){ return {ok:false,error:"network"}; });
}
var ASH=null;
function setToast(t){
  var el=document.getElementById("toast");
  if(!el){el=document.createElement("div");el.id="toast";el.className="toast";document.body.appendChild(el);}
  el.textContent=t;el.classList.add("on");
  clearTimeout(setToast._t);setToast._t=setTimeout(function(){el.classList.remove("on");},1900);
}
window.__TOAST=setToast;
function openAsh(o){
  ASH=o||{};
  var sh=document.getElementById("actSheet"); if(!sh)return;
  document.getElementById("ashT").textContent=o.title||"";
  document.getElementById("ashD").textContent=o.desc||"";
  var f=document.getElementById("ashF"),i=document.getElementById("ashI");
  if(o.field){
    f.style.display="flex"; i.value=""; i.placeholder=o.field.placeholder||"";
    i.setAttribute("inputmode",o.field.mode||"text");
    i.setAttribute("maxlength",o.field.max||12);
  }else{ f.style.display="none"; }
  document.getElementById("ashE").textContent="";
  var ok=document.getElementById("ashOk");
  ok.textContent=o.okText||"确认";
  ok.className="ashOk"+(o.danger?" danger":"");
  sh.classList.add("on"); sh.setAttribute("aria-hidden","false");
}
function closeAsh(){
  var sh=document.getElementById("actSheet"); if(!sh)return;
  sh.classList.remove("on"); sh.setAttribute("aria-hidden","true");
  ASH=null;
}
if(!window.__SETBOUND){
  window.__SETBOUND=1;
  document.addEventListener("click",function(e){
    var x=e.target.closest("[data-ashclose]"); if(x){closeAsh();return;}
    var okb=e.target.closest("#ashOk");
    if(okb){
      var o=ASH||{};
      var inp=document.getElementById("ashI");
      var _v=(inp.value||"").trim();
      if(o.field&&o.field.required!==false&&!_v){
        document.getElementById("ashE").textContent=o.error||"请填写后再提交";return;
      }
      if(o.field&&o.field.kind==="code"&&_v&&(_v.length>32||/\s/.test(_v))){
        document.getElementById("ashE").textContent="安全码不能含空格，最多 32 个字符";return;
      }
      if(o.post){
        var eb=document.getElementById("ashE"), okb2=okb, oldTxt=okb.textContent;
        eb.textContent=""; okb.disabled=true; okb.textContent="提交中…";
        acctPost(o.post,_v).then(function(j){
          okb.disabled=false; okb.textContent=oldTxt;
          if(!j||!j.ok){ eb.textContent=ACCT_ERR[j&&j.error]||"操作失败，请稍后再试"; return; }
          if(o.after)o.after();
          if(j.pwd){ copyText(j.pwd,function(){}); closeAsh(); setToast("新密码 "+j.pwd+"（已复制）"); return; }
          closeAsh(); setToast(j.message||o.done||"已提交");
        });
        return;
      }
      if(o.after)o.after();
      closeAsh(); setToast(o.done||"已提交"); return;
    }
    var r=e.target.closest("[data-act]"); if(!r)return;
    var a=r.getAttribute("data-act");
    if(a==="pwd") openAsh({title:"重置密码",desc:"验证安全码后，系统会生成一个新密码。请立即在 Emby 客户端用新密码登录并修改。",
      field:{placeholder:"输入安全码",mode:"text",kind:"code",max:32},okText:"确认重置",post:"reset",done:"密码已重置"});
    else if(a==="code"&&D.codeChanged){ setToast("每账号仅可修改一次，需要重置请联系管理员"); }
    else if(a==="code") openAsh({title:"修改安全码",desc:"支持数字、字母或符号，1–32 个字符，不能含空格。修改后不可再次更改，请妥善保存。",
      field:{placeholder:"请输入新安全码",mode:"text",kind:"code",max:32},okText:"保存",post:"code",done:"安全码已更新",
      after:function(){ D.codeChanged=true; renderSet(); }});
    else if(a==="rebind") openAsh({title:"换绑 Telegram",desc:"填写新的 Telegram 数字 ID（不是 @用户名）。换绑后该账号归新 TG 所有，请谨慎操作。",
      field:{placeholder:"例如 123456789",mode:"text",max:15},okText:"确认换绑",post:"rebind",done:"已换绑，请用新账号重新打开"});
    else if(a==="del") openAsh({title:"删除账号",desc:"将永久删除 Emby 账号与全部观看记录，无法恢复。输入安全码以确认。",
      field:{placeholder:"输入安全码",mode:"text",kind:"code",max:32},okText:"永久删除",danger:1,post:"delete",done:"账号已删除"});
    else if(a==="contact"){ copyText("@JingCaiDiJiaBot",function(ok){ setToast(ok?"已复制 @JingCaiDiJiaBot":"复制失败，请手动记下"); }); }
    else if(a==="rules"){ if(window.openFs) openFs("fsRules"); }
    else if(a==="dev"){ if(window.openFs) openFs("fsDev"); }
    else if(a==="line"){ go("pgLine"); }
    else if(a==="live"){ tickLive(); setToast("实时观看人数已刷新"); }
    else if(a==="about"){ setToast("ChapandaBot v2.0.0"); }
    else if(a==="sys"){ if(window.openFs) openFs("fsSys"); }
    else if(a==="shop"){ if(window.openFs) openFs("fsMall"); }
  });
  document.addEventListener("keydown",function(e){
    if(e.key==="Escape"&&ASH) closeAsh();
    if(e.key==="Enter"&&ASH&&e.target&&e.target.id==="ashI"){ var b=document.getElementById("ashOk"); if(b)b.click(); }
  });
}
function copyText(t,cb){
  function fallback(){
    try{
      var ta=document.createElement("textarea"); ta.value=t;
      ta.style.cssText="position:fixed;top:-200px;opacity:0";
      document.body.appendChild(ta); ta.select();
      var ok=document.execCommand("copy"); document.body.removeChild(ta); cb(ok);
    }catch(e){ cb(false); }
  }
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(t).then(function(){cb(true);},fallback);
  }else fallback();
}

renderSet();

renderRank();renderDomains();loadSign();renderMedal();
(function(){
  var tabs=document.getElementById("rankTabs"), chips=document.getElementById("rankChips");
  if(tabs)tabs.addEventListener("click",function(e){
    var b=e.target.closest("button"); if(!b)return;
    [].forEach.call(tabs.children,function(x){x.classList.toggle("on",x===b);});
    board=b.getAttribute("data-board"); renderRank();
  });
  if(chips)chips.addEventListener("click",function(e){
    var b=e.target.closest("button"); if(!b)return;
    [].forEach.call(chips.children,function(x){x.classList.toggle("on",x===b);});
    span=b.getAttribute("data-span"); renderRank();
  });
})();
if($("mMore"))$("mMore").addEventListener("click",openMedal);
if($("medalCard"))$("medalCard").addEventListener("click",function(e){if(e.target.closest("button"))return;openMedal();});
document.querySelectorAll("[data-close]").forEach(function(el){el.addEventListener("click",closeMedal);});
if($("mList"))$("mList").addEventListener("click",function(){closeMedal();});
var hs=location.hash.replace("#","");
if(hs&&document.getElementById(hs))go(hs);
window.MC={D:D,MEDALS:MEDALS,curLevel:curLevel,medalImg:medalImg,
  refresh:function(){ location.reload(); }};
})();

/* 顶栏底部 + 10px 作为内容起始位置（safe-area-inset 各机型不一致，直接量） */
function fitTop(){
  var tb=document.querySelector('.topbar');if(!tb)return;
  var b=Math.round(tb.getBoundingClientRect().bottom);
  if(b>0) document.documentElement.style.setProperty('--top-pad',(b+10)+'px');
}
fitTop();
window.addEventListener('resize',fitTop);
window.addEventListener('orientationchange',function(){setTimeout(fitTop,120);});
setTimeout(fitTop,60);setTimeout(fitTop,300);

/* ===== 全屏页：我的账户 / 我的设备 / 我的称号 ===== */
(function(){
  var $=function(s){return document.getElementById(s)};
  var _mcAva=(window.MC&&window.MC.avaFill)||null;
  var avaFill=_mcAva||function(el,src,name){
    if(!el)return; el.textContent=String(name||"k").charAt(0).toUpperCase();
    if(!src)return; var im=new Image();
    im.onload=function(){el.innerHTML="";el.appendChild(im);}; im.alt=""; im.src=src;
  };
  var MC=window.MC||{}, D=MC.D||{name:"kaixin",level:"a",credit:156,unit:"茶叶",
    watch30:42.5,watchTotal:43.5,conc:{now:1,limit:4}};
  var MEDALS=MC.MEDALS||[];
  function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];});}
  function h1(n){return (Math.round(n*10)/10).toString().replace(/\.0$/,"");}

  /* ---------- 打开 / 关闭 ---------- */
  function openFs(id){
    var el=$(id); if(!el) return;
    if(id==="fsAcct") renderAcct();
    if(id==="fsDev")  renderDev();
    if(id==="fsMedal")renderMedalFs();
    if(id==="fsMall" && window.__renderMall) window.__renderMall();
    if(id==="fsBag"  && window.__renderBag)  window.__renderBag();
    if(id==="fsSys"  && window.__renderSys)  window.__renderSys();
    var all=document.querySelectorAll(".fs");
    for(var i=0;i<all.length;i++){all[i].classList.remove("on");all[i].setAttribute("aria-hidden","true");}
    el.classList.add("on"); el.setAttribute("aria-hidden","false");
    var b=el.querySelector(".fsBody"); if(b) b.scrollTop=0;
    document.body.classList.add("fsOpen");
  }
  window.__OPENFS=openFs;
  function closeFs(){
    var all=document.querySelectorAll(".fs");
    for(var i=0;i<all.length;i++){all[i].classList.remove("on");all[i].setAttribute("aria-hidden","true");}
    document.body.classList.remove("fsOpen");
  }
  window.openFs=openFs; window.closeFs=closeFs;

  /* ---------- 我的账户 ---------- */
  function row(k,v,opt){
    opt=opt||{};
    return '<div class="frow'+(opt.act?' act':'')+'"'+(opt.go?' data-go="'+opt.go+'"':'')+
      '><span>'+esc(k)+'</span><b'+(opt.tone?' class="'+opt.tone+'"':'')+'>'+v+
      (opt.go?' <i class="chev">›</i>':'')+'</b></div>';
  }
  function renderAcct(){
    var vip=D.level==="a";
    avaFill($("acAva"), D.avatar, D.name);
    if($("acName"))$("acName").textContent=D.name||"kaixin";
    if($("acSub")) $("acSub").textContent="Emby 账户 · "+(vip?"尊贵白名单":"普通用户");
    if($("acState")){$("acState").textContent=D.state||"正常";$("acState").className="fpill ok";}
    var info=[
      row("Emby 用户名",esc(D.embyName||"—")),
      row("账户权益",vip?"尊贵白名单":"普通用户",{tone:vip?"vip":"blue"}),
      row("账户状态",esc(D.state||"正常"),{tone:"ok"}),
      row("注册时间",esc(D.created||"2026-08-15")),
      row("会员到期",vip?'永久有效':(esc(D.expire||"—")+(D.days!=null?' <i class="dim">（'+D.days+" 天）</i>":''))),
      row("并发路数",(D.conc?D.conc.now+"/"+D.conc.limit:"1/4")),
      row("奶茶",(D.credit==null?0:D.credit)+' <i class="dim">'+(D.unit||"奶茶")+'</i>'),
      row("累计观看",h1(D.watchTotal||0)+' 小时'),
      row("近 30 天",h1(D.watch30||0)+' 小时')
    ].join("");
    function safeHtml(){
      var it=DEV.items||[], last=it[0]||null;
      return [
        row("登录密码","已设置",{tone:"ok"}),
        row("最近登录", last?esc((last.dev||"未知设备")+(last.ip?(" · "+last.ip):"")):"暂无记录",{}),
        row("最近登录时间", last?esc(when(last.last)):"—"),
        row("登录设备",(DEV.count||0)+" 台 · "+(DEV.ips||0)+" 个 IP",{act:true,go:"fsDev"})
      ].join("");
    }
    if($("acInfo"))$("acInfo").innerHTML=info;
    if($("acSafe"))$("acSafe").innerHTML=safeHtml();
    fetchDevices(function(){ if($("acSafe"))$("acSafe").innerHTML=safeHtml(); });
  }

  /* ---------- 我的设备（真实：/app/api/devices，与机器人「我的设备」同源） ---------- */
  var DEV={items:[],count:0,ips:0,loaded:false,loading:false};
  function pad2(n){return n<10?("0"+n):(""+n);}
  function when(iso){
    if(!iso) return "—";
    var d=new Date(iso); if(isNaN(d.getTime())) return String(iso).replace("T"," ").slice(0,16);
    var now=new Date(), diff=Math.max(0,now-d);
    var m=Math.floor(diff/60000);
    if(m<1) return "刚刚";
    if(m<60) return m+" 分钟前";
    var hh=Math.floor(m/60);
    if(hh<24 && d.getDate()===now.getDate()) return hh+" 小时前";
    var h=pad2(d.getHours()), mi=pad2(d.getMinutes());
    if(d.getFullYear()===now.getFullYear() && d.getMonth()===now.getMonth() && d.getDate()===now.getDate()-1) return "昨天 "+h+":"+mi;
    return pad2(d.getMonth()+1)+"-"+pad2(d.getDate())+" "+h+":"+mi;
  }
  function fetchDevices(cb){
    if(DEV.loading){ if(cb) DEV._cb=cb; return; }
    DEV.loading=true;
    fetch("api/devices",{headers:{"X-Tg-Init-Data":(window.__INIT||"")}})
      .then(function(r){return r.json();})
      .then(function(j){
        if(j&&j.ok){ DEV.items=j.items||[]; DEV.count=j.count||0; DEV.ips=j.ips||0; DEV.loaded=true; }
      })
      .catch(function(){})
      .then(function(){ DEV.loading=false; var c=cb||DEV._cb; DEV._cb=null; if(c)c(); });
  }
  function devHtml(d){
    var kick=d.id?('<button class="devKick" type="button" data-sid="'+esc(d.id)+'">下线</button>'):'';
    return '<div class="dev"><div class="devL"><i class="devDot'+(d.playing?" on":"")+'"></i>'+
      '<div><b>'+esc(d.dev||"未知设备")+'</b><span>'+esc([d.app,d.n?("播放 "+d.n+" 次"):""].filter(Boolean).join(" · "))+'</span></div></div>'+
      '<div class="devR"><b>'+esc(d.ip||"—")+'</b><span>'+esc(d.playing&&d.title?("正在播放 · "+d.title):when(d.last))+'</span></div>'+kick+'</div>';
  }
  /* 实时踢下线 */
  var KICKING={};
  function kickDev(btn){
    var sid=btn.getAttribute("data-sid"); if(!sid||KICKING[sid])return;
    KICKING[sid]=1; btn.disabled=true; btn.textContent="下线中";
    fetch("api/devices/kick",{method:"POST",cache:"no-store",
      headers:{"Content-Type":"application/json","X-Tg-Init-Data":(window.__INIT||"")},
      body:JSON.stringify({id:sid})})
      .then(function(r){return r.json();})
      .then(function(j){
        delete KICKING[sid];
        if(j&&j.ok){ if(window.__TOAST)window.__TOAST("已下线，该设备播放已中断"); }
        else { btn.disabled=false; btn.textContent="下线"; if(window.__TOAST)window.__TOAST((j&&j.hint)||"下线失败，请重试"); }
        DEV.loaded=false; fetchDevices(renderDev);
      })
      .catch(function(){ delete KICKING[sid]; btn.disabled=false; btn.textContent="下线"; if(window.__TOAST)window.__TOAST("网络异常，请重试"); });
  }
  function renderDev(){
    var it=DEV.items||[], live=it.filter(function(x){return x.playing;}), hist=it.filter(function(x){return !x.playing;});
    if($("devLive")) $("devLive").innerHTML=live.length?live.map(devHtml).join(""):'<div class="devEmpty">当前没有正在播放的设备</div>';
    if($("devHist")) $("devHist").innerHTML=hist.length?hist.map(devHtml).join(""):'<div class="devEmpty">'+(DEV.loaded?"暂无播放记录":"加载中…")+'</div>';
    if($("devLiveCnt"))$("devLiveCnt").textContent=live.length+" 台";
    if($("devHistCnt"))$("devHistCnt").textContent=(DEV.count||0)+" 台 · "+(DEV.ips||0)+" 个 IP";
    if(!DEV.loaded && !DEV.loading) fetchDevices(renderDev);
  }
  function renderDevAsync(){
    renderDev();
    fetchDevices(renderDev);
  }
  if($("fsDev")) $("fsDev").addEventListener("click",function(e){
    var b=e.target.closest?e.target.closest(".devKick"):null;
    if(!b)return; e.preventDefault(); kickDev(b);
  });

  /* ---------- 我的称号 ---------- */
  function renderMedalFs(){
    var tot=D.watchTotal||0, lv=0, cur=null, next=null;
    for(var i=0;i<MEDALS.length;i++){ if(tot>=MEDALS[i].h){lv=MEDALS[i].i;cur=MEDALS[i];} else {next=MEDALS[i];break;} }
    if(!cur){cur={i:0,name:"新人观众",h:0};lv=0;next=MEDALS[0];}
    var span=(next?next.h:cur.h)-(cur.h||0);
    var pct=next&&span>0?Math.min(100,Math.round((tot-cur.h)/span*100)):100;
    var hero=$("mdHero");
    if(hero){
      hero.innerHTML=
        '<div class="mdHeroTop">'+
          '<div class="mdLv">'+
            '<div class="mdLvBig">Lv'+(lv||"0")+'</div>'+

          '</div>'+
        '</div>'+
        '<div class="mdHeroMid">'+ (MC.medalImg?MC.medalImg(lv||1,"mdBig"):'') +
          '<div class="mdHtx">'+
            '<div class="mdName">'+esc(cur.name)+'</div>'+
            '<div class="mdSub">累计观看 '+h1(tot)+' 小时'+
              (next?' · 距 <b>'+esc(next.name)+'</b> 还差 <b>'+h1(next.h-tot)+'</b> 小时':' · 已达最高等级')+'</div>'+
          '</div>'+
        '</div>'+
        '<div class="mdBar"><i style="width:'+pct+'%"></i></div>'+
        '<div class="mdPct"><span>当前 Lv'+(lv||0)+' · 上限 Lv'+MEDALS.length+'</span><span>'+pct+'%</span></div>';
    }
    var rows=[];
    for(var j=0;j<MEDALS.length;j++){
      var m=MEDALS[j], prev=j?MEDALS[j-1]:null;
      var done=tot>=m.h, isNext=(!done && (j===0 || tot>=MEDALS[j-1].h));
      var p=100;
      if(!done){
        var base=prev?prev.h:0;
        p=Math.max(0,Math.min(100,Math.round((tot-base)/(m.h-base)*100)));
      }
      rows.push('<div class="lvrow '+(done?"done":(isNext?"now":"lock"))+'">'+
        (MC.medalImg?MC.medalImg(m.i):"")+
        '<div class="lvMid">'+
          '<div class="lvNm">'+esc(m.name)+'</div>'+
          '<div class="lvMeta">'+(done?'已解锁 · ':(isNext?'还差 '+h1(m.h-tot)+' 小时 · ':'解锁需 '))+m.h+' 小时</div>'+
          '<div class="lvProg"><i style="width:'+p+'%"></i></div>'+
        '</div>'+
        '<div class="lvSt '+(done?"done":(isNext?"now":""))+'">'+(done?"已获得":(isNext?p+"%":"未解锁"))+'</div>'+
      '</div>');
    }
    if($("lvList"))$("lvList").innerHTML=rows.join("");
  }

  /* ---------- 事件 ---------- */
  document.addEventListener("click",function(e){
    var b=e.target.closest?e.target.closest("[data-fsclose]"):null;
    if(b){e.preventDefault();closeFs();return;}
    var g=e.target.closest?e.target.closest("[data-go]"):null;
    if(g){e.preventDefault();var id=g.getAttribute("data-go");(id==="fsDev"?openFs:closeFs)(id);}
  });
  document.addEventListener("keydown",function(e){if(e.key==="Escape")closeFs();});

  function bind(id,fs){
    var el=$(id);
    if(!el) return;
    el.addEventListener("click",function(e){e.preventDefault();openFs(fs);});
  }
  bind("qAcct","fsAcct"); bind("qDev","fsDev"); bind("qMedal","fsMedal");
  bind("qMedal2","fsMedal");

  if(window.__AUTOFS) openFs(window.__AUTOFS);
})();


/* ---------- 全屏页统一关闭按钮 ---------- */
(function(){
  try{
    var hs=document.querySelectorAll(".fs .fsHd");
    for(var i=0;i<hs.length;i++){
      (function(h){
        if(h.querySelector(".hdClose")) return;
        var b=document.createElement("button");
        b.type="button"; b.className="hdClose"; b.setAttribute("aria-label","关闭");
        b.textContent="关闭";
        b.addEventListener("click",function(e){
          e.preventDefault(); e.stopPropagation();
          if(window.closeFs) window.closeFs();
        });
        h.appendChild(b);
      })(hs[i]);
    }
  }catch(e){}
})();

/* 全屏页内点击底栏：先关闭全屏页再切页 */
document.addEventListener("click",function(e){
  var t=e.target.closest?e.target.closest(".tabbar .tab"):null;
  if(t&&window.closeFs)closeFs();
},true);

/* 线路页「使用规范」置顶图：点击看大图 */
(function(){
  var c=document.getElementById("rulesCard");
  if(c) c.addEventListener("click",function(){ if(window.openFs) openFs("fsRules"); });
})();
/* ========== 兑换码（对应 EmbyBoss 机器人「兑换」） ========== */
(function(){
  var $x=function(i){return document.getElementById(i);};
  var ICO={
    clock:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.4"/><path d="M12 7.6V12l3 1.9"/></svg>',
    crown:'<svg viewBox="0 0 24 24"><path d="M3.6 18h16.8l-1.1-9-4.4 3.1L12 6.4 9.1 12.1 4.7 9l-1.1 9z"/></svg>',
    lib:'<svg viewBox="0 0 24 24"><rect x="4" y="4" width="7" height="7" rx="1.6"/><rect x="13" y="4" width="7" height="7" rx="1.6"/><rect x="4" y="13" width="7" height="7" rx="1.6"/><rect x="13" y="13" width="7" height="7" rx="1.6"/></svg>',
    gift:'<svg viewBox="0 0 24 24"><rect x="3.8" y="8.6" width="16.4" height="11.6" rx="2.2"/><path d="M3.8 12.6h16.4M12 8.6v11.6"/><path d="M12 8.6c-2.6 0-4.4-.9-4.4-2.5S8.4 3.9 9.7 4.4c1.3.5 2.3 4.2 2.3 4.2s1-3.7 2.3-4.2c1.3-.5 2.1.1 2.1 1.7s-1.8 2.5-4.4 2.5z"/></svg>',
    tag:'<svg viewBox="0 0 24 24"><path d="M4 11.2V5.2c0-.7.5-1.2 1.2-1.2h6l8.4 8.4-7.2 7.2L4 11.2z"/><circle cx="8.2" cy="8.2" r="1.4"/></svg>',
    unlock:'<svg viewBox="0 0 24 24"><rect x="4.4" y="10.6" width="15.2" height="9.4" rx="2.4"/><path d="M8 10.6V7.8a4 4 0 017.6-1.7"/><circle cx="12" cy="15.3" r="1.3"/></svg>'
  };
  var SKU_ICO={renew:"clock",whitelist:"crown",partition:"lib",invite:"gift",title:"tag",unban:"unlock"};
  var KINDS=[
    {sku:"whitelist",n:"白名单卡",p:8888,ico:ICO.crown},
    {sku:"invite",n:"邀请码卡",p:300,ico:ICO.gift},
    {sku:"title",n:"自定义称号卡",p:166,ico:ICO.tag},
    {sku:"unban",n:"解封卡",p:100,ico:ICO.unlock}
  ];
  function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];});}
  function tile(it){
    var ic=it.icon?('<span class="emo">'+esc(it.icon)+'</span>'):(it.ico||ICO[SKU_ICO[it.sku]]||ICO.gift);
    return '<div class="shopTile" data-open-shop="1" data-sku="'+esc(it.sku)+'">'+
      '<div class="stIc">'+ic+'</div>'+
      '<div class="stN">'+esc(it.n)+'</div>'+
      '<div class="stP"><b>'+esc(it.p)+'</b></div></div>';
  }
  function renderHot(items){
    var box=$x("shopRow"); if(!box)return;
    var list=(items||[]).filter(function(i){return i&&i.on_sale;})
      .sort(function(a,b){return (a.sort||0)-(b.sort||0);})
      .slice(0,8)
      .map(function(i){return {sku:i.sku,n:i.name,p:i.price,ico:(ICO[SKU_ICO[i.sku]]||ICO.gift)};});
    if(!list.length) list=KINDS;
    box.innerHTML=list.map(tile).join("");
  }
  function loadHot(){
    fetch("api/mall",{credentials:"same-origin",cache:"no-store",
      headers:{"X-Tg-Init-Data":(window.__INIT||"")}})
      .then(function(r){return r.json();})
      .then(function(j){
        if(!j||!j.ok) return;
        renderHot(j.items);
        if(j.bal!=null){ window.__MBAL=j.bal; syncBal(); }
      })
      .catch(function(){});
  }
  function D(){return (window.MC&&MC.D)||{};}
  function unit(){return D().unit||"奶茶";}
  function syncBal(){
    var c=D().credit; if(c==null&&window.__MBAL!=null)c=window.__MBAL; if(c==null)return;
    if($x("balNum"))$x("balNum").textContent=c;
    if($x("shopBal"))$x("shopBal").textContent=c;
  }
  function msg(t,ok){
    var el=$x("rdMsg"); if(!el)return;
    el.textContent=t||"";
    el.className="rdMsg"+(t?(ok?" ok":" err"):"");
  }
  function mapErr(j){
    var M={code_used:"该兑换码已被使用",code_not_found:"兑换码不存在，请检查后重试",
      code_invalid:"兑换码无效",already_wl:"您已是白名单用户，无需重复激活",
      no_account:"请先在机器人注册 Emby 账号",register_code:"这是注册码，您已有账户，无需使用",
      whitelist_closed:"管理员暂未开启白名单码",save_failed:"保存失败，请稍后重试",
      bad_code:"请输入兑换码",unauthorized:"身份校验失败，请从 Telegram 重新打开"};
    return M[(j&&j.error)||""]||(j&&j.hint)||"兑换失败，请稍后再试";
  }
  function redeem(){
    var inp=$x("rdCode"), btn=$x("rdBtn"); if(!inp||!btn)return;
    var code=(inp.value||"").trim();
    if(!code){ msg("请输入兑换码",false); inp.focus(); return; }
    btn.disabled=true; btn.textContent="兑换中…"; msg("");
    fetch("api/redeem",{method:"POST",cache:"no-store",
      headers:{"Content-Type":"application/json","X-Tg-Init-Data":(window.__INIT||"")},
      body:JSON.stringify({code:code})})
      .then(function(r){return r.json();})
      .then(function(j){
        btn.disabled=false; btn.textContent="立即兑换";
        if(j&&j.ok){
          var t="兑换成功";
          if(j.kind==="renew") t="续期成功 · "+j.days+" 天，有效期至 "+j.expire;
          else if(j.kind==="whitelist") t="白名单已激活 · 永久有效";
          else if(j.kind==="partition") t="分区「"+(j.partition||"")+"」已解锁"+(j.expire?(" · 至 "+j.expire):"");
          msg(t,true); inp.value=""; inp.blur();
          if(window.MC&&MC.refresh) MC.refresh();
          if(window.__BAG__&&window.__BAG__.load) window.__BAG__.load();
        }else{ msg(mapErr(j),false); }
      })
      .catch(function(){
        btn.disabled=false; btn.textContent="立即兑换";
        msg("网络异常，请稍后再试",false);
      });
  }
  document.addEventListener("click",function(e){
    var go=e.target.closest?e.target.closest("[data-open-shop],#shopGo,#shopMore"):null;
    if(go){e.preventDefault(); if(window.__OPENFS) window.__OPENFS("fsMall");}
  });
  var b=$x("rdBtn"); if(b) b.addEventListener("click",redeem);
  var i=$x("rdCode"); if(i) i.addEventListener("keydown",function(e){
    if(e.key==="Enter"||e.keyCode===13){e.preventDefault();redeem();}
  });
  renderHot(null); syncBal(); loadHot();
  window.__SHOP__={sync:syncBal};
})();

/* ========== Emby 账号（用户名 / 密码，带复制） ========== */
(function(){
  var shown=false;
  function toast(t){ if(window.__TOAST) return window.__TOAST(t); }
  function D(){return (window.MC&&MC.D)||{};}
  function mask(v){ return v?("•".repeat(Math.min(10,Math.max(6,String(v).length)))):"••••••"; }
  function fill(){
    var d=D(), u=document.getElementById("credUser"), p=document.getElementById("credPwd");
    if(u) u.textContent=(d.embyUser||d.embyName||"—");
    if(p) p.textContent=shown?((d.embyPwd&&String(d.embyPwd))||"—"):mask(d.embyPwd);
  }
  document.addEventListener("click",function(e){
    var t=e.target.closest?e.target.closest("#cpUser,#cpPwd,#eyePwd"):null;
    if(!t)return;
    e.preventDefault();
    var id=t.id, d=D();
    if(id==="eyePwd"){ shown=!shown; t.textContent=shown?"隐藏":"显示"; fill(); return; }
    var v=id==="cpUser"?(d.embyUser||d.embyName||""):(d.embyPwd||"");
    if(!v){ toast("暂未获取到信息"); return; }
    var ok=false;
    try{
      var ta=document.createElement("textarea"); ta.value=String(v); ta.setAttribute("readonly","");
      ta.style.cssText="position:fixed;top:-200px;left:0;opacity:0";
      document.body.appendChild(ta); ta.select(); ta.setSelectionRange(0,String(v).length);
      ok=document.execCommand("copy"); document.body.removeChild(ta);
    }catch(err){ ok=false; }
    if(!ok&&navigator.clipboard&&navigator.clipboard.writeText){
      try{ navigator.clipboard.writeText(String(v)); ok=true; }catch(err2){}
    }
    toast(ok?(id==="cpUser"?"用户名已复制":"密码已复制"):"复制失败，请手动选择");
  });
  document.addEventListener("click",function(e){
    var t=e.target.closest?e.target.closest(".tabbar .tab[data-pg='pgLine']"):null;
    if(t) setTimeout(fill,60);
  });
  fill();
  window.MC&&(window.MC.fillCred=fill);
})();

/* 底栏高度 → CSS 变量，内容留白只算一份 */
function fitChrome(){
  var tb=document.querySelector('.tabbar');
  if(!tb) return;
  var h=Math.round(tb.getBoundingClientRect().height);
  if(h>0) document.documentElement.style.setProperty('--tab-h',h+'px');
}
window.addEventListener('load',fitChrome);
window.addEventListener('resize',fitChrome);
window.addEventListener('orientationchange',function(){setTimeout(fitChrome,180);});
setTimeout(fitChrome,60);setTimeout(fitChrome,320);

/* ========== 首页资源统计（今日更新剧集 / 今日更新电影 / 总资源） ========== */
(function(){
  function el(id){ return document.getElementById(id); }
  function fmt(n){
    n=Number(n)||0;
    return n.toLocaleString("en-US");
  }
  function load(){
    var e1=el("libEp"); if(!e1) return;
    fetch("api/stats",{cache:"no-store",headers:{"X-Tg-Init-Data":(window.__INIT||"")}})
      .then(function(r){ return r.json(); })
      .then(function(j){
        if(!j||!j.ok) return;
        e1.textContent=fmt(j.eps);
        if(el("libMv")) el("libMv").textContent=fmt(j.movies);
        if(el("libAll")) el("libAll").textContent=fmt(j.total);
        var bar=el("libBar");
        if(bar){
          bar.classList.add("done");
          var tip="统计时间 "+new Date().toLocaleString("zh-CN",{hour12:false});
          [].forEach.call(bar.children,function(c){ if(c.title==="") c.title=tip; });
        }
      })
      .catch(function(){});
  }
  window.__loadStats=load;
  load();
  setTimeout(load,60000);
})();

/* ===== 首页三按钮：奶茶明细 / 近30天观影 / 正在播放 ===== */
(function(){
  function $(id){return document.getElementById(id);}
  function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];});}
  function setRing(id,p){var el=$(id); if(el) el.style.setProperty("--p",Math.max(0,Math.min(100,p||0)));}
  function hm(sec){sec=Math.max(0,Math.round(sec||0));
    var h=Math.floor(sec/3600), m=Math.round((sec%3600)/60);
    if(h>0) return h+" 小时"+(m?" "+m+" 分":"");
    if(m>0) return m+" 分钟";
    return sec+" 秒";}
  function hh(sec){return (Math.max(0,sec||0)/3600).toFixed(1);}
  function ms(sec){sec=Math.max(0,Math.round(sec||0));var h=Math.floor(sec/3600),m=Math.floor(sec%3600/60),x=sec%60;
    return (h?h+":":"")+(h?String(m).padStart(2,"0"):m)+":"+String(x).padStart(2,"0");}
  function get(u){ return fetch(u,{credentials:"same-origin",cache:"no-store",
    headers:{"X-Tg-Init-Data":(window.__INIT||"")}}).then(function(r){return r.json();}); }
  var TITLE={points:"奶茶明细",watch:"近 30 天观影",playing:"正在播放"};
  var CUR="points", BUSY=0;

  function body(html){ var b=$("spBody"); if(b) b.innerHTML=html; }

  function renderPoints(d){
    var items=(d&&d.items)||[];
    var plus=0, minus=0;
    items.forEach(function(i){ var v=Number(i.d||0); if(v>0) plus+=v; else minus+=v; });
    var h='<div class="spSum"><b>'+(d&&d.bal!=null?d.bal:0)+'</b><span>奶茶余额</span>'+
      '<em>本月 +'+plus+' / '+minus+'</em></div>';
    if(!items.length){
      h+='<div class="spList"><div class="spEmpty">还没有奶茶变动记录<br><span style="font-size:11px;opacity:.8">签到、求片、兑换都会记在这里</span></div></div>';
    } else {
      h+='<div class="spList">'+items.map(function(i){
        var v=Number(i.d||0), up=v>0;
        var ic=i.src==="checkin"?"签":(i.src==="request"?"片":"礼");
        var srcTxt=i.src==="checkin"?"每日签到":(i.src==="request"?"求片消耗":"其他");
        return '<div class="spRow">'+
          '<div class="spIc '+(up?"up":"dn")+'">'+ic+'</div>'+
          '<div class="spTx"><div class="spT">'+esc(i.note||srcTxt)+'</div>'+
          '<div class="spS">'+esc((i.ts||"").slice(0,16))+' · '+srcTxt+'</div></div>'+
          '<div class="spV'+(up?"":" dn")+'">'+(up?"+":"")+v+'</div></div>';
      }).join("")+'</div>';
    }
    h+='<div class="spFoot">奶茶来源：每日签到<br>更早的余额变动没有逐笔记录，只保留当前余额</div>';
    if(!SILENT) body(h);
    if(d&&d.bal!=null) setRing("rgCredit", Math.min(100, Number(d.bal||0)/10));
  }

  function renderWatch(d){
    var items=(d&&d.items)||[], tot=d&&d.total||0;
    var h='<div class="spSum"><b>'+hh(tot)+'</b><span>小时 · 近 30 天</span>'+
      '<em>'+items.length+' 部</em></div>';
    if(!items.length){
      h+='<div class="spList"><div class="spEmpty">近 30 天还没有观看记录</div></div>';
    } else {
      h+='<div class="spList">'+items.map(function(i,k){
        var pct=Math.max(3,Math.round(Number(i.sec||0)/Math.max(1,Number(items[0].sec||1))*100));
        return '<div class="spRow">'+
          '<div class="spIc">'+(k+1)+'</div>'+
          '<div class="spTx"><div class="spT">'+esc(i.name||"—")+'</div>'+
          '<div class="spS">'+hm(i.sec)+(i.last?" · 最近 "+esc(String(i.last).slice(5,10)):"")+
          (i.eps>1?" · "+i.eps+" 集":"")+'</div>'+
          '<div class="spBar" style="margin-top:6px"><i style="width:'+pct+'%"></i></div></div>'+
          '<div class="spV">'+hh(i.sec)+'h</div></div>';
      }).join("")+'</div>';
    }
    h+='<div class="spFoot">按剧集汇总播放时长（已扣除暂停）</div>';
    if(!SILENT) body(h);
    setRing("rgWatch", Math.min(100, hh(tot)/30*100));
  }

  function renderPlaying(d){
    var items=(d&&d.items)||[], lim=(d&&d.limit)||1;
    var h='<div class="spSum"><b>'+items.length+'</b><span>路正在播放 · 上限 '+lim+' 路</span>'+
      '<em>'+(items.length?"实时":"空闲")+'</em></div>'+
      '<div class="spLv"><span>普通用户 <b>'+(d&&d.lv_n?d.lv_n:1)+'</b> 路</span>'+
      '<span>白名单用户 <b>'+(d&&d.lv_w?d.lv_w:4)+'</b> 路</span>'+
      '<span>你：<b>'+esc((d&&d.lv_name)||"普通")+'</b></span></div>';
    if(!items.length){
      h+='<div class="spList"><div class="spEmpty">当前没有正在播放的会话<br>'+
        '<span style="font-size:11px;opacity:.8">在别的设备上开始播放后会出现在这里</span></div></div>';
    } else {
      h+=items.map(function(i){
        var p=Math.max(0,Math.min(100,Number(i.pct||0)));
        return '<div class="spPl">'+
          '<div class="spPlTop"><span class="spDot"></span>'+
          '<span class="spDev">'+esc(i.dev||"未知设备")+'</span>'+
          '<span class="spApp">'+esc(i.app||"")+'</span></div>'+
          '<div class="spName">'+esc(i.title||"—")+'</div>'+
          '<div class="spBar"><i style="width:'+p+'%"></i></div>'+
          '<div class="spPct"><span>'+ms(i.pos)+' / '+ms(i.dur)+'</span>'+
          '<span>'+(i.paused?"已暂停":p+'%')+'</span></div>'+
          '<button class="spKill" type="button" data-kill="'+esc(i.sid)+'">结束这台设备的播放</button>'+
          '</div>';
      }).join("");
    }
    h+='<div class="spFoot">结束播放会立即断开该设备，并释放一路并发</div>';
    if(!SILENT) body(h);
    setRing("rgConc", lim? Math.min(100, items.length/lim*100) : 0);
    if($("sConc")) $("sConc").innerHTML=items.length+'<i>/'+lim+'</i>';
    if($("concLbl")) $("concLbl").textContent="正在播放";
    var box=$("concBox");
    if(box) box.classList.toggle("on-live", items.length>0);
  }

  var MOCK={
    points:{ok:1,bal:156,plus:276,minus:120,untracked:0,items:[
      {ts:"2026-10-26 08:12",d:10,src:"checkin",note:"每日签到"},
      {ts:"2026-10-25 21:40",d:-30,src:"request",note:"求片：沙丘2"},
      {ts:"2026-10-25 08:03",d:10,src:"checkin",note:"每日签到"},
      {ts:"2026-10-24 08:01",d:10,src:"checkin",note:"每日签到"},
      {ts:"2026-10-23 19:22",d:-60,src:"request",note:"求片：繁花"},
      {ts:"2026-10-23 08:00",d:10,src:"checkin",note:"每日签到"}]},
    watch:{ok:1,total:156600,total_hm:"43.5",items:[
      {id:"1",name:"繁花",sec:46200,eps:30,last:"2026-10-25"},
      {id:"2",name:"沙丘2",sec:39600,eps:1,last:"2026-10-24"},
      {id:"3",name:"三体",sec:28800,eps:12,last:"2026-10-22"},
      {id:"4",name:"狂飙",sec:21600,eps:8,last:"2026-10-20"},
      {id:"5",name:"奥本海默",sec:12600,eps:1,last:"2026-10-18"}]},
    playing:{ok:1,count:2,limit:4,lv_n:1,lv_w:4,lv_name:"白名单",items:[
      {sid:"a1",dev:"iPhone 15 Pro Max",app:"CapyPlayer",title:"仙逆",name:"第 42 集",sub:"S01E42",pos:1320,dur:1440,pct:92,paused:false,playing:true,remote:"1.2.3.4"},
      {sid:"a2",dev:"iPad Air",app:"EplayerX",title:"繁花",name:"第 18 集",sub:"S01E18",pos:540,dur:2700,pct:20,paused:true,playing:true,remote:"1.2.3.4"}]}
  };

  var SILENT=false;
  function load(which, silent){
    var t=++BUSY;
    SILENT=!!silent;
    if(!SILENT) body('<div class="spEmpty">加载中…</div>');
    if(window.__MOCK_STAT && MOCK[which]){
      setTimeout(function(){
        if(t!==BUSY) return;
        if(which==="points") renderPoints(MOCK.points);
        else if(which==="watch") renderWatch(MOCK.watch);
        else renderPlaying(MOCK.playing);
      },180);
      return;
    }
    var url = which==="points" ? "api/points/log"
            : which==="watch" ? "api/watch/top?days=30" : "api/playing";
    get(url).then(function(d){
      if(t!==BUSY) return;
      if(which==="points") renderPoints(d);
      else if(which==="watch") renderWatch(d);
      else renderPlaying(d);
    }).catch(function(){
      if(t!==BUSY) return;
      body('<div class="spEmpty">加载失败，请下拉重试</div>');
    });
  }

  function open(which){
    CUR=which;
    if($("spTitle")) $("spTitle").textContent=TITLE[which]||"详情";
    if(window.__OPENFS) window.__OPENFS("fsStat");
    load(which);
  }

  document.addEventListener("click",function(e){
    var cell=e.target.closest?e.target.closest("#statsBar [data-sp]"):null;
    if(cell){ e.preventDefault(); open(cell.getAttribute("data-sp")); return; }
    var k=e.target.closest?e.target.closest(".spKill"):null;
    if(k&&!k.disabled){
      var sid=k.getAttribute("data-kill"), dev=k.closest(".spPl");
      if(!sid) return;
      k.disabled=true; k.textContent="正在结束…";
      fetch("api/playing/stop",{method:"POST",credentials:"same-origin",
        headers:{"Content-Type":"application/json","X-Tg-Init-Data":(window.__INIT||"")},
        body:JSON.stringify({id:sid})})
      .then(function(r){return r.json();})
      .then(function(d){
        if(d&&d.ok){ k.textContent="已结束"; k.classList.add("spKillOk");
          if(window.__TOAST) window.__TOAST("已结束播放");
          setTimeout(function(){ load("playing"); },700);
        } else {
          k.disabled=false; k.textContent="结束失败，点此重试";
        }
      }).catch(function(){ k.disabled=false; k.textContent="结束失败，点此重试"; });
    }
  });

  // 首屏静默刷新一次在播数（只更新环和数字，不动面板内容）
  setTimeout(function(){ load("playing", true); },520);

  // 预览：自动打开统计面板
  if(window.__OPEN_PANEL){
    setTimeout(function(){ open(window.__OPEN_PANEL); },260);
  }
})();

/* 点顶栏 logo 刷新当前页面（保留当前 tab 与滚动位置） */
(function(){
  var h=document.getElementById("topBar");
  if(!h) return;
  var busy=false;
  h.classList.add("logoTap");
  h.addEventListener("click",function(){
    if(busy) return; busy=true;
    h.classList.add("spinning");
    try{ if(navigator.vibrate) navigator.vibrate(8); }catch(e){}
    try{
      var p=document.querySelector(".page.on");
      sessionStorage.setItem("mb_pg", p?p.id:"pgHome");
      sessionStorage.setItem("mb_sy", String(window.scrollY||window.pageYOffset||0));
    }catch(e){}
    setTimeout(function(){ location.reload(); },200);
  });
})();

/* 刷新后回到原页面 */
(function(){
  var pg=null,sy=0;
  try{
    pg=sessionStorage.getItem("mb_pg");
    sy=parseInt(sessionStorage.getItem("mb_sy")||"0",10)||0;
    sessionStorage.removeItem("mb_pg"); sessionStorage.removeItem("mb_sy");
  }catch(e){ return; }
  if(!pg) return;
  var userMoved=false;
  document.addEventListener("click",function(e){
    if(e.isTrusted&&e.target.closest&&e.target.closest(".tabbar .tab")) userMoved=true;
  },true);
  function apply(){
    var t=document.querySelector('.tab[data-pg="'+pg+'"]');
    if(!t) return;
    var cur=document.querySelector(".page.on");
    if(cur&&cur.id===pg) return;
    t.click();
    if(sy>0) setTimeout(function(){ window.scrollTo(0,sy); },180);
  }
  function boot(){
    setTimeout(apply,140);
    setTimeout(function(){
      if(userMoved) return;
      var cur=document.querySelector(".page.on");
      if(cur&&cur.id===pg) return;
      apply();
    },820);
  }
  if(document.readyState==="loading") document.addEventListener("DOMContentLoaded",boot);
  else boot();
})();

/* ==================== 商城 & 系统设置 ==================== */
(function(){
  var INIT=window.__INIT||"";
  function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];});}
  function jget(u){
    return fetch(u,{credentials:"same-origin",cache:"no-store",
      headers:{"X-Tg-Init-Data":tk()}}).then(function(r){return r.json();});
  }
  function jpost(u,b){
    return fetch(u,{method:"POST",credentials:"same-origin",cache:"no-store",
      headers:{"Content-Type":"application/json","X-Tg-Init-Data":INIT},
      body:JSON.stringify(b||{})}).then(function(r){return r.json();});
  }
  function $(i){return document.getElementById(i);}
  function toast(t){ if(window.setToast) window.setToast(t); }

  /* ---------------- 商城 ---------------- */
  /* ---------------- 图标（04 圆角方块 · 蓝色线性） ---------------- */
  var MICO={
    clock:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.4"/><path d="M12 7.6V12l3 1.9"/></svg>',
    crown:'<svg viewBox="0 0 24 24"><path d="M3.6 18h16.8l-1.1-9-4.4 3.1L12 6.4 9.1 12.1 4.7 9l-1.1 9z"/></svg>',
    lib:'<svg viewBox="0 0 24 24"><rect x="4" y="4" width="7" height="7" rx="1.6"/><rect x="13" y="4" width="7" height="7" rx="1.6"/><rect x="4" y="13" width="7" height="7" rx="1.6"/><rect x="13" y="13" width="7" height="7" rx="1.6"/></svg>',
    gift:'<svg viewBox="0 0 24 24"><rect x="3.8" y="8.6" width="16.4" height="11.6" rx="2.2"/><path d="M3.8 12.6h16.4M12 8.6v11.6"/><path d="M12 8.6c-2.6 0-4.4-.9-4.4-2.5S8.4 3.9 9.7 4.4c1.3.5 2.3 4.2 2.3 4.2s1-3.7 2.3-4.2c1.3-.5 2.1.1 2.1 1.7s-1.8 2.5-4.4 2.5z"/></svg>',
    tag:'<svg viewBox="0 0 24 24"><path d="M4 11.2V5.2c0-.7.5-1.2 1.2-1.2h6l8.4 8.4-7.2 7.2L4 11.2z"/><circle cx="8.2" cy="8.2" r="1.4"/></svg>',
    unlock:'<svg viewBox="0 0 24 24"><rect x="4.4" y="10.6" width="15.2" height="9.4" rx="2.4"/><path d="M8 10.6V7.8a4 4 0 017.6-1.7"/><circle cx="12" cy="15.3" r="1.3"/></svg>'
  };
  var MSKU={renew:"clock",whitelist:"crown",partition:"lib",invite:"gift",title:"tag",unban:"unlock"};
  function mico(sku){ return MICO[MSKU[sku]]||MICO.gift; }

  var M={items:[],bal:0,admin:false,mode:false,loaded:false,busy:0,pending:{}};
  var ERR={
    no_bal:"奶茶不足", out_of_stock:"已售罄", off_sale:"该商品已下架",
    no_item:"商品不存在", forbidden:"没有权限", not_banned:"账号未被封禁",
    already_wl:"你已经是白名单了", bad_note:"称号需 2-12 个字",
    buy_failed:"兑换失败，奶茶已退回", not_registered:"账号未开通", unauthorized:"身份校验失败"
  };
  function eMsg(d){ return (d&&d.hint)||ERR[(d&&d.error)||""]||"操作失败，请稍后再试"; }

  function stockTag(it){
    return '<span class="mcStock'+(it.stock<=0?" zero":"")+'">'+(it.stock>0?("剩 "+it.stock):"售罄")+'</span>';
  }
  function card(it){
    var off=!it.on_sale, zero=it.stock<=0;
    var top='<div class="mcTop"><span class="mcIco">'+mico(it.sku)+'</span><span class="mcName">'+esc(it.name)+'</span>'+stockTag(it)+'</div>';
    var desc='<div class="mcDesc">'+esc(it.descr||"")+'</div>';
    if(M.mode){
      var h='<div class="mc'+(off?" off":"")+'" data-sku="'+esc(it.sku)+'">';
      h+=top+desc;
      h+='<div class="mcAdmin">'+
        '<label>价格<input type="number" inputmode="numeric" min="0" data-f="price" value="'+it.price+'"></label>'+
        '<label>库存<input type="number" inputmode="numeric" min="0" data-f="stock" value="'+it.stock+'"></label>'+
        '</div>';
      if(it.sku==="renew"||it.sku==="invite"||it.sku==="partition"){
        h+='<div class="mcAdmin" style="grid-template-columns:1fr"><label>天数<input type="number" inputmode="numeric" min="1" data-f="days" value="'+((it.payload&&it.payload.days)||30)+'"></label></div>';
      }
      if(it.sku==="partition"){
        h+='<div class="mcAdmin" style="grid-template-columns:1fr"><label>分区名<input type="text" data-f="partition" value="'+esc((it.payload&&it.payload.partition)||"")+'" placeholder="如 4K"></label></div>';
      }
      h+='<div class="mcSw'+(it.on_sale?" on":"")+'" data-f="on_sale"><i></i><span>'+(it.on_sale?"已上架":"已下架")+'</span></div>';
      h+='<div class="mcBot"><span class="mcPrice"><b>'+it.price+'</b>奶茶</span><button class="mcBuy" data-act="save" type="button">保存</button></div>';
      return h+'</div>';
    }
    var tip="";
    if(it.sku==="title"){ tip='<div class="mcForm" style="display:none" data-note="1"><input class="mcInp" type="text" maxlength="12" placeholder="输入你的称号（2-12 字）"></div>'; }
    var btn='<button class="mcBuy'+(off||zero?" dis":"")+'" data-act="buy" type="button"'+((off||zero)?' disabled':'')+'>'+
      (off?"已下架":(zero?"售罄":"兑换"))+'</button>';
    return '<div class="mc tk'+(off?" off":"")+'" data-sku="'+esc(it.sku)+'">'+
      '<div class="mcIn">'+
        '<div class="mcMain">'+top+desc+tip+'</div>'+
        '<div class="mcStub"><span class="mcPrice"><b>'+it.price+'</b>奶茶</span>'+btn+'</div>'+
      '</div></div>';
  }
  function renderMall(){
    var box=$("mallList"); if(!box)return;
    box.className="mallGrid"+(M.mode?" admin":" one");
    box.innerHTML=M.items.map(card).join("")||'<div class="mallEmpty">暂无商品</div>';
    window.__MBAL=M.bal;
    if($("mallBal")) $("mallBal").textContent=M.bal;
    if($("balNum")) $("balNum").textContent=M.bal;
    if($("mallModeBtn")){ $("mallModeBtn").hidden=!M.admin; $("mallModeBtn").className="miniBtn"+(M.mode?" on":""); }
    if($("mallOrdCnt")) $("mallOrdCnt").textContent=M.orders&&M.orders.length?("共 "+M.orders.length+" 笔"):"";
    renderOrders();
  }
  function renderOrders(){
    var box=$("mallOrd"); if(!box)return;
    var list=M.orders||[];
    if(!list.length){ box.innerHTML='<div class="mallEmpty">还没有兑换记录</div>'; return; }
    box.innerHTML=list.map(function(o){
      var nm=esc(o.name||o.sku), rs=esc(o.result||"");
      return '<div class="mo"><b>'+nm+'</b><em>-'+o.price+'</em>'+
        (rs?'<span class="moR">'+rs+'</span>':'')+'</div>';
    }).join("");
  }
  function loadMall(cb){
    jget("api/mall").then(function(d){
      if(!d||!d.ok){ toast(d&&d.error==="unauthorized"?"身份校验失败":eMsg(d)); return; }
      M.items=d.items||[]; M.bal=d.bal||0; M.admin=!!d.admin; 
      if(!M.admin) M.mode=false;
      M.loaded=true; renderMall();
      if(cb)cb();
    }).catch(function(){ toast("网络异常，请重试"); });
  }
  function loadOrders(){
    jget("api/mall/orders").then(function(d){ if(d&&d.ok){ M.orders=d.items||[]; renderOrders(); } }).catch(function(){});
  }
  function findSku(sku){ for(var i=0;i<M.items.length;i++){ if(M.items[i].sku===sku) return M.items[i]; } return null; }

  function buy(sku,btn,note){
    if(M.busy) return; M.busy=1; btn.disabled=true;
    var old=btn.textContent; btn.textContent="兑换中…";
    jpost("api/mall/buy",{sku:sku,note:note||""}).then(function(d){
      M.busy=0; btn.disabled=false;
      if(d&&d.ok){
        if(d.bal!=null){ M.bal=d.bal; if($("mallBal"))$("mallBal").textContent=d.bal; }
        toast(d.msg||"兑换成功");
        if(d.code){
          jget("api/bag").then(function(bd){
            var its=(bd&&bd.ok&&bd.unused)||[], ln="", i;
            for(i=0;i<its.length;i++){
              if(its[i].sku==="invite"&&String(its[i].result)===String(d.code)){ ln=String(its[i].link||""); break; }
            }
            if(!ln){ for(i=0;i<its.length;i++){ if(its[i].sku==="invite"&&its[i].link){ ln=String(its[i].link); break; } } }
            if(ln&&copyText(ln)) toast("邀请链接已复制，直接发给好友即可");
            else toast("邀请码已生成，请在背包复制链接");
            if(window.__BAG__&&__BAG__.load) __BAG__.load();
            if(window.__OPENFS) window.__OPENFS("fsBag");
          }).catch(function(){ if(window.__OPENFS) window.__OPENFS("fsBag"); });
        }
        else if(window.__BAG__&&window.__BAG__.load) window.__BAG__.load();
        loadMall(); loadOrders();
        if(window.refreshProfile) window.refreshProfile();
      }else{
        btn.textContent=old;
        toast(eMsg(d));
      }
    }).catch(function(){ M.busy=0; btn.disabled=false; btn.textContent=old; toast("网络异常，请重试"); });
  }

  function saveCard(el){
    var sku=el.getAttribute("data-sku"); var it=findSku(sku); if(!it)return;
    var body={sku:sku};
    var ins=el.querySelectorAll("[data-f]");
    for(var i=0;i<ins.length;i++){
      var f=ins[i].getAttribute("data-f");
      if(f==="on_sale"){ body.on_sale=ins[i].classList.contains("on"); continue; }
      if(f==="partition"){ body.partition=ins[i].value.trim(); continue; }
      var v=parseInt(ins[i].value,10); if(isNaN(v)) continue;
      body[f]=v;
    }
    var b=el.querySelector('[data-act="save"]'); if(b){ b.disabled=true; b.textContent="保存中…"; }
    jpost("api/mall/save",body).then(function(d){
      if(d&&d.ok){ toast("已保存"); M.items=d.items||M.items; renderMall(); }
      else{ toast(eMsg(d)); if(b){ b.disabled=false; b.textContent="保存"; } }
    }).catch(function(){ toast("网络异常"); if(b){ b.disabled=false; b.textContent="保存"; } });
  }

  document.addEventListener("click",function(e){
    var t=e.target; if(!t||!t.closest) return;
    var sw=t.closest(".mcSw");
    if(sw){ sw.classList.toggle("on"); var sp=sw.querySelector("span");
      if(sp) sp.textContent=sw.classList.contains("on")?"已上架":"已下架"; return; }
    var btn=t.closest("[data-act]");
    if(btn){
      var el=btn.closest(".mc"); if(!el) return;
      var sku=el.getAttribute("data-sku");
      if(btn.getAttribute("data-act")==="save"){ saveCard(el); return; }
      var it=findSku(sku); if(!it) return;
      if(it.sku==="title"){
        var wrap=el.querySelector('[data-note]');
        if(!wrap) return;
        if(wrap.style.display==="none"){ wrap.style.display="flex"; wrap.querySelector("input").focus(); 
          btn.textContent="确认兑换"; return; }
        var note=wrap.querySelector("input").value.trim();
        if(note.length<2||note.length>12){ toast("请输入 2-12 个字的称号"); return; }
        buy(sku,btn,note); return;
      }
      if(M.pending[sku]){ M.pending[sku]=0; buy(sku,btn,""); }
      else{
        M.pending[sku]=1; btn.textContent="确认兑换";
        setTimeout(function(){ if(M.pending[sku]){ M.pending[sku]=0; if(document.body.contains(btn)) btn.textContent="兑换"; } },4000);
      }
      return;
    }
    if(t.closest("#mallCodeBtn")){ if(window.openFs) window.openFs("fsBag"); return; }
    if(t.closest("#mallModeBtn")){ M.mode=!M.mode; renderMall(); toast(M.mode?"管理模式下可改价格与库存":"已退出管理模式"); return; }
  });

  /* ---------------- 我的背包 ---------------- */
  var BAG={unused:[],used:[],bal:0,tab:"unused",loaded:false};
  function copyText(txt){
    try{
      if(navigator.clipboard&&navigator.clipboard.writeText){ navigator.clipboard.writeText(txt); return true; }
    }catch(e){}
    try{
      var ta=document.createElement("textarea"); ta.value=txt;
      ta.style.cssText="position:fixed;top:-200px;opacity:0"; document.body.appendChild(ta);
      ta.select(); ta.setSelectionRange(0,99999);
      var ok=document.execCommand("copy"); document.body.removeChild(ta); return ok;
    }catch(e){ return false; }
  }
  function bagFind(id){
    var all=BAG.unused.concat(BAG.used);
    for(var i=0;i<all.length;i++) if(String(all[i].id)===String(id)) return all[i];
    return null;
  }
  function bagItem(it){
    var used=!!it.used, r=String(it.result||""), at=String(it.at||""), inv=(it.sku==="invite");
    var lk=String(it.link||"");
    var act=inv
      ? '<button class="biBtn" type="button" data-bagcopy="'+it.id+'">复制</button>'
      : (used ? '<span class="biTag">已使用</span>'
              : '<button class="biBtn" type="button" data-baguse="'+it.id+'">使用</button>');
    var sub=inv
      ? ('<span class="biCode">'+esc(lk?lk.replace(/^https?:\/\//,""):r)+'</span>')
      : (r?('<span>'+esc(r)+'</span>'):'');
    return '<div class="bagIt'+(used?" used":"")+'" data-bagid="'+it.id+'">'+
        '<span class="biIco">'+mico(it.sku)+'</span>'+
        '<span class="biM"><b>'+esc(it.name||it.sku)+'</b>'+sub+
          (at?('<span>'+esc(at)+'</span>'):'')+
        '</span>'+act+
      '</div>';
  }
  function renderBag(){
    var box=$("bagList"); if(!box) return;
    var list=(BAG.tab==="unused")?BAG.unused:BAG.used;
    box.innerHTML=list.length?list.map(bagItem).join(""):
      ('<div class="mallEmpty">'+(BAG.tab==="unused"?"背包里还没有未使用的物品":"还没有使用记录")+'</div>');
    var nu=$("bagNU"), u2=$("bagU2");
    if(nu) nu.textContent=BAG.unused.length;
    if(u2) u2.textContent=BAG.used.length;
    if($("bagCnt")) $("bagCnt").textContent=(BAG.unused.length+BAG.used.length)?("共 "+(BAG.unused.length+BAG.used.length)+" 件"):"";
    var tabs=document.querySelectorAll("#bagTabs .bagTab");
    for(var i=0;i<tabs.length;i++) tabs[i].classList.toggle("on", tabs[i].getAttribute("data-bag")===BAG.tab);
    if($("shopBal")) $("shopBal").textContent=BAG.bal;
    window.__MBAL=BAG.bal;
  }
  function loadBag(cb){
    jget("api/bag").then(function(d){
      if(!d||!d.ok){ toast(eMsg(d)); return; }
      BAG.unused=d.unused||[]; BAG.used=d.used||[]; BAG.bal=d.bal||0; BAG.loaded=true;
      renderBag(); if(cb)cb();
    }).catch(function(){ toast("网络异常，请重试"); });
  }
  function bagUse(id,btn){
    if(!btn||btn.disabled) return;
    btn.disabled=true; btn.textContent="使用中…";
    jpost("api/bag/use",{id:Number(id)}).then(function(d){
      if(d&&d.ok){
        if(d.copy){
          var cok=copyText(String(d.copy));
          btn.disabled=false; btn.textContent="复制";
          toast(cok?"邀请链接已复制，发给好友即可":"请长按邀请码手动复制");
          return;
        }
        var i,it=null;
        for(i=0;i<BAG.unused.length;i++){ if(String(BAG.unused[i].id)===String(id)){ it=BAG.unused.splice(i,1)[0]; break; } }
        if(it){ it.used=1; BAG.used.unshift(it); }
        toast(d.result?("已使用 · "+d.result):"已使用");
        BAG.tab="used";
        renderBag();
      }else{
        btn.disabled=false; btn.textContent="使用"; toast(eMsg(d));
      }
    }).catch(function(){ btn.disabled=false; btn.textContent="使用"; toast("网络异常，请重试"); });
  }
  window.__renderBag=function(){ loadBag(); };
  window.__BAG__={load:loadBag,use:bagUse};
  (function(){ var f=$("fsBag"); if(f&&f.classList.contains("on")) loadBag(); })();

  document.addEventListener("click",function(e){
    var t=e.target; if(!t||!t.closest) return;
    if(t.closest("[data-open-bag]")||t.closest("#mallBagEntry")){
      e.preventDefault(); if(window.__OPENFS) window.__OPENFS("fsBag"); return;
    }
    if(t.closest("#hotRedeem")){
      e.preventDefault(); if(window.__OPENFS) window.__OPENFS("fsMall"); return;
    }
    var tb=t.closest("#bagTabs .bagTab");
    if(tb){ e.preventDefault(); BAG.tab=tb.getAttribute("data-bag")||"unused"; renderBag(); return; }
    var ub=t.closest("[data-baguse]");
    if(ub){ e.preventDefault(); bagUse(ub.getAttribute("data-baguse"),ub); return; }
    var cb=t.closest("[data-bagcopy]");
    if(cb){
      e.preventDefault();
      var ci=bagFind(cb.getAttribute("data-bagcopy"));
      var cl=ci?String(ci.link||("https://t.me/?start="+String(ci.result||""))):"";
      if(!cl){ toast("邀请链接暂不可用"); return; }
      var cok=copyText(cl);
      toast(cok?"邀请链接已复制，发给好友即可":"请长按下面的邀请码复制");
      return;
    }
  },false);

  window.__renderMall=function(){ if(!M.loaded){ loadMall(); loadOrders(); } else { renderMall(); loadMall(); loadOrders(); } };

  /* ---------------- 系统设置 ---------------- */
  var S={sys:{},admin:false,ver:"2.0.0",loaded:false};
  function sysHtml(){
    var s=S.sys, ro=S.admin?"":" readonly", dis=S.admin?"":" disabled";
    var h='<section class="sec glass">'+
      '<div class="fsSecHd">站点公告<span class="cnt">显示在首页顶部</span></div>'+
      '<div class="sysField"><label>公告标题</label><input id="syAnnT" maxlength="40" value="'+esc(s.ann_title||"")+'" placeholder="如 服务器维护通知"'+ro+'></div>'+
      '<div class="sysField"><label>公告内容</label><textarea id="syAnnC" maxlength="400" placeholder="留空则不显示公告条"'+ro+'>'+esc(s.ann_text||"")+'</textarea></div>'+
      '<button class="sysSave" id="sySave"'+dis+'>保存公告</button></section>';
    h+='<section class="sec glass">'+
      '<div class="fsSecHd">客服与联系</div>'+
      '<div class="sysField"><label>客服账号</label><input id="sySup" maxlength="60" value="'+esc(s.support||"")+'" placeholder="@JingCaiDiJiaBot"'+ro+'></div>'+
      '<button class="sysSave" id="sySave2"'+dis+'>保存客服</button></section>';
    h+='<section class="sec glass">'+
      '<div class="fsSecHd">运行状态</div>'+
      '<div class="mtRow"><div><div class="mtT">维护模式</div><div class="mtS">开启后仅管理员可进入小程序</div></div>'+
        '<div class="mtSw'+((s.maintenance==="1")?" on":"")+'" id="syMt" role="button" data-admin="'+(S.admin?1:0)+'"></div></div>'+
      '<div class="mtRow"><div class="mtT">版本</div><div class="mtS">v'+esc(S.ver)+'</div></div>'+
      '<div class="mtRow"><div class="mtT">在线人数</div><div class="mtS" id="syLive">—</div></div>'+
      '<div class="mtRow"><div class="mtT">资源总数</div><div class="mtS" id="syRes">—</div></div>'+
      '</section>';
    h+='<p class="fsTip">'+(S.admin?"修改后立即对全部用户生效":"仅管理员可修改，这里可以查看当前配置")+'</p>';
    return h;
  }
  function renderSys(){
    var box=$("sysBody"); if(!box)return;
    box.innerHTML=sysHtml();
    if(S.admin){
      $("sySave").onclick=function(){ saveSys({"ann_title":$("syAnnT").value,"ann_text":$("syAnnC").value},this); };
      $("sySave2").onclick=function(){ saveSys({"support":$("sySup").value},this); };
      $("syMt").onclick=function(){
        var on=!this.classList.contains("on");
        this.classList.toggle("on",on);
        saveSys({"maintenance":on?"1":"0"},null,on?"维护模式已开启":"维护模式已关闭");
      };
    }
    jget("api/stats").then(function(d){ if(d&&d.ok&&$("syRes")) $("syRes").textContent=(d.total||0).toLocaleString()+" 部"; }).catch(function(){});
    jget("api/live").then(function(d){ if(d&&d.ok&&$("syLive")) $("syLive").textContent=(d.now!=null?d.now:0)+" 人"; }).catch(function(){});
  }
  function saveSys(body,btn,msg){
    if(btn){ btn.disabled=true; var o=btn.textContent; btn.textContent="保存中…"; }
    jpost("api/sys/save",body).then(function(d){
      if(btn){ btn.disabled=false; btn.textContent="保存"; }
      if(d&&d.ok){ if(d.sys) S.sys=d.sys; toast(msg||"已保存"); applySys(); }
      else toast(eMsg(d));
    }).catch(function(){ if(btn){ btn.disabled=false; btn.textContent="保存"; } toast("网络异常"); });
  }
  function loadSys(cb){
    jget("api/sys").then(function(d){
      if(!d||!d.ok) return;
      S.sys=d.sys||{}; S.admin=!!d.admin; S.ver=d.ver||"2.0.0"; S.loaded=true;
      if(cb)cb();
    }).catch(function(){});
  }
  window.__renderSys=function(){ if(!S.loaded) loadSys(renderSys); else renderSys(); };

  /* 首页公告条 + 维护模式 */
  function applySys(){
    var s=S.sys||{}, t=(s.ann_text||"").trim();
    var old=$("annBar"); if(old) old.parentNode.removeChild(old);
    if(t){
      var home=$("pgHome"); if(home){
        var d=document.createElement("section");
        d.className="sec glass annBar"; d.id="annBar";
        d.innerHTML='<div class="annT">'+esc((s.ann_title||"站点公告"))+'</div><div class="annC">'+esc(t)+'</div>';
        home.insertBefore(d, home.firstChild.nextSibling||home.firstChild);
      }
    }
    var gate=$("mtGate");
    if(s.maintenance==="1" && !S.admin){
      if(!gate){
        gate=document.createElement("div"); gate.id="mtGate";
        gate.innerHTML='<div class="mtCard"><div class="mtIco">🔧</div><b>系统维护中</b>'+
          '<span>'+esc(t||"我们正在升级服务，请稍后再来")+'</span>'+
          '<em>客服：'+esc(s.support||"@JingCaiDiJiaBot")+'</em></div>';
        document.body.appendChild(gate);
      }
    }else if(gate){ gate.parentNode.removeChild(gate); }
  }
  window.__applySys=applySys;

  loadSys(function(){ applySys(); });
})();

/* ==================== 记住上次所在页面（一级 + 二级） ==================== */
(function(){
  var K="mi_nav_v2", MAXAGE=6*3600*1000;
  function curPg(){var p=document.querySelector(".page.on");return p?p.id:"pgHome";}
  function curFs(){var f=document.querySelector(".fs.on");return f?f.id:"";}
  function save(){
    try{localStorage.setItem(K,JSON.stringify({pg:curPg(),fs:curFs(),t:Date.now()}));}catch(e){}
  }
  window.__NAVSAVE=save;
  document.addEventListener("click",function(e){
    if(!e.isTrusted) return;
    setTimeout(save,300);
  },true);
  window.addEventListener("pagehide",save);
  document.addEventListener("visibilitychange",function(){
    if(document.visibilityState==="hidden") save();
  });
  function apply(){
    var st=null;
    try{ st=JSON.parse(localStorage.getItem(K)||"null"); }catch(e){}
    if(!st||!st.pg) return;
    if(st.t && (Date.now()-st.t)>MAXAGE) return;
    if(st.pg!==curPg()){
      var tb=document.querySelector('.tab[data-pg="'+st.pg+'"]');
      if(tb && !tb.disabled && !tb.classList.contains("dis")) tb.click();
    }
    if(st.fs && !curFs() && window.__OPENFS){
      setTimeout(function(){ if(!curFs()) window.__OPENFS(st.fs); },360);
    }
  }
  function boot(){ setTimeout(apply,240); setTimeout(apply,1000); }
  if(document.readyState==="loading") document.addEventListener("DOMContentLoaded",boot);
  else boot();
})();
/* 顶栏实时在线 + 热播详情（随 app.js 一起加载） */
(function(){
  var $=function(id){return document.getElementById(id)};
  function tk(){ try{ return window.__INIT || new URLSearchParams(location.search).get("initData") || ""; }catch(e){ return window.__INIT||""; } }

  /* ---------- 顶栏留白同步 ---------- */
  var bar=$("topBar");
  function syncPad(){
    if(!bar) return;
    var h=Math.round(bar.getBoundingClientRect().height||0);
    if(h>0) document.documentElement.style.setProperty("--top-pad",(h+10)+"px");
  }
  syncPad();
  [80,240,700,1600].forEach(function(t){setTimeout(syncPad,t)});
  if(window.ResizeObserver&&bar){
    try{ new ResizeObserver(syncPad).observe(bar); }catch(e){}
  }
  window.addEventListener("orientationchange",function(){setTimeout(syncPad,260)});
  window.addEventListener("resize",syncPad);

  /* ---------- 在线 / 在看 ---------- */
  function num(n){ return (n>=10000)?(n/10000).toFixed(1)+"万":String(n); }
  function bump(el,v){
    if(!el) return;
    var t=num(v);
    if(el.textContent===t) return;
    el.textContent=t;
    el.classList.remove("bump");
    void el.offsetWidth;
    el.classList.add("bump");
  }
  var liveBusy=false, liveTimer=null;
  function fetchLive(){
    if(liveBusy) return;
    liveBusy=true;
    fetch("api/live",{cache:"no-store",headers:{"X-Tg-Init-Data":tk()}})
      .then(function(r){return r.json()})
      .then(function(d){
        liveBusy=false;
        if(!d||!d.ok) return;
        var s=$("liveStrip"); if(s&&s.hidden) s.hidden=false;
        bump($("lvOnline"), d.online|0);
        bump($("lvWatch"), (d.watching!=null?d.watching:d.now)|0);
        syncPad();
      }).catch(function(){liveBusy=false;});
  }
  function startLive(){ fetchLive(); if(!liveTimer) liveTimer=setInterval(fetchLive,60000); }
  setTimeout(startLive,200);
  [1200,3000,6000,12000,20000].forEach(function(ms){ setTimeout(fetchLive,ms); });
  document.addEventListener("visibilitychange",function(){ if(!document.hidden) fetchLive(); });

  /* ---------- 热播详情 ---------- */
  var hotBtn=null;
  function fmtSpan(l){ return l||""; }
  function openHot(name,cnt,label){
    var fs=$("fsHot"); if(!fs||!window.__OPENFS) return;
    if($("hdName")) $("hdName").textContent=name||"—";
    if($("hdChips")) $("hdChips").innerHTML="";
    if($("hdCnt")) $("hdCnt").textContent=(label?label+" · ":"")+(cnt?("播放 "+cnt+" 次"):"");
    var im=$("hdImg"); if(im){ im.removeAttribute("src"); }
    if($("hdOv")) $("hdOv").textContent="简介加载中…";
    hotBtn=$("hdPlay"); if(hotBtn){hotBtn.disabled=true;hotBtn.dataset.id="";}
    window.__OPENFS("fsHot");
    fetch("api/hot/detail?n="+encodeURIComponent(name||""),{cache:"no-store",headers:{"X-Tg-Init-Data":tk()}})
      .then(function(r){return r.json()})
      .then(function(d){
        if(!d||!d.ok){ if($("hdOv")) $("hdOv").textContent="这部暂时拿不到简介，去 Emby 里搜一下吧。"; return; }
        if($("hdName")) $("hdName").textContent=d.name||name;
        var chips=[];
        if(d.year) chips.push('<span class="hdCh">'+d.year+'</span>');
        if(d.type) chips.push('<span class="hdCh">'+(d.type==="Series"?"剧集":"电影")+'</span>');
        if(cnt) chips.push('<span class="hdCh hot">'+(label?label+" ":"")+cnt+' 次播放</span>');
        if($("hdChips")) $("hdChips").innerHTML=chips.join("");
        if($("hdOv")) $("hdOv").textContent=d.ov||"暂无简介。";
        var im2=$("hdImg");
        if(im2&&d.poster){ im2.src=d.poster; im2.alt=d.name||""; }
        if(hotBtn){ hotBtn.disabled=!d.id; hotBtn.dataset.id=d.id||""; }
      }).catch(function(){
        if($("hdOv")) $("hdOv").textContent="网络不太顺，稍后再试。";
      });
  }
  window.__OPENHOT=openHot;

  document.addEventListener("click",function(e){
    var t=e.target;
    while(t&&t!==document){
      if(t.classList&&t.classList.contains("hk")){
        openHot(t.getAttribute("data-hn")||"", t.getAttribute("data-hc")||"", t.getAttribute("data-hl")||"");
        return;
      }
      t=t.parentNode;
    }
  });

  function embyOpen(id){
    if(!id) return;
    var url="https://emby.example.com/web/index.html#!/item?id="+encodeURIComponent(id);
    try{
      if(window.Telegram&&Telegram.WebApp&&Telegram.WebApp.openLink){ Telegram.WebApp.openLink(url); return; }
    }catch(e){}
    window.open(url,"_blank");
  }
  var btn=document.getElementById("hdPlay");
  if(btn) btn.addEventListener("click",function(){ embyOpen(btn.dataset.id); });

  setInterval(function(){
    if(document.hidden) return;
    syncPad();
  },20000);
})();

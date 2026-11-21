(() => {
  const byId = (id) => document.getElementById(id);
  const startBtn = byId("track-start");
  if (!startBtn) return;
  const stopBtn = byId("track-stop"), status = byId("location-status");
  const coords = byId("coords"), detectedLabel = byId("detected-facility");
  const visitStartedLabel = byId("visit-started"), visitDurationLabel = byId("visit-duration");
  const facilityData = byId("facility-data");
  const facilities = JSON.parse(facilityData.dataset.facilities || "[]");
  const initialActive = JSON.parse(facilityData.dataset.active || "null");
  let watchId = null, ticker = null, latest = null, activeFacilityId = initialActive?.facility_id || null;
  let insideCandidateId = null, insideSince = null, outsideSince = null, lastHeartbeat = 0;
  let activeStartedAt = initialActive?.started_at ? new Date(initialActive.started_at) : null;
  let tracking = false, searchMarkers = [];

  const localStamp = (d = new Date()) => {
    const pad = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  };
  const fmt = seconds => [Math.floor(seconds/3600), Math.floor(seconds%3600/60), seconds%60].map(v=>String(v).padStart(2,"0")).join(":");
  const distance = (a,b) => {
    const rad=x=>x*Math.PI/180, R=6371000;
    const dLat=rad(b.latitude-a.latitude), dLon=rad(b.longitude-a.longitude);
    const x=Math.sin(dLat/2)**2+Math.cos(rad(a.latitude))*Math.cos(rad(b.latitude))*Math.sin(dLon/2)**2;
    return 2*R*Math.asin(Math.sqrt(x));
  };
  async function postJson(url, body={}) {
    const response = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json", "X-CSRFToken":window.CSRF_TOKEN}, body:JSON.stringify(body)});
    const data = await response.json().catch(()=>({}));
    if (!response.ok || data.ok === false) throw new Error(data.error || "サーバーへの保存に失敗しました。");
    return data;
  }
  function updateActiveLabels() {
    const facility = facilities.find(f=>f.id===activeFacilityId);
    detectedLabel.textContent = facility ? `${facility.name}（${facility.activity}）` : "なし";
    visitStartedLabel.textContent = activeStartedAt ? activeStartedAt.toLocaleString("ja-JP") : "--";
    visitDurationLabel.textContent = activeStartedAt ? fmt(Math.max(0,Math.floor((Date.now()-activeStartedAt.getTime())/1000))) : "00:00:00";
  }
  async function beginVisit(facility) {
    const data = await postJson("/api/facilities/visit/start", {facility_id:facility.id, timestamp:localStamp()});
    activeFacilityId=facility.id;
    if(data.status === "started" || !activeStartedAt) activeStartedAt=new Date();
    outsideSince=null; insideCandidateId=null; insideSince=null;
    updateActiveLabels();
    status.textContent=`「${facility.name}」での滞在を検知しました。活動「${facility.activity}」を記録中です。`;
  }
  async function endVisit() {
    if(!activeFacilityId) return;
    const facility=facilities.find(f=>f.id===activeFacilityId);
    const data=await postJson("/api/facilities/visit/end", {timestamp:localStamp()});
    const result=data.result;
    status.textContent=result ? `「${result.facility}」の実績を保存しました（${result.minutes}分）。` : "滞在記録を終了しました。";
    activeFacilityId=null; activeStartedAt=null; outsideSince=null; insideCandidateId=null; insideSince=null;
    updateActiveLabels();
  }
  function processPosition(position) {
    latest=position;
    const p=position.coords;
    coords.textContent=`${p.latitude.toFixed(6)}, ${p.longitude.toFixed(6)}（精度 約${Math.round(p.accuracy)}m）`;
    if(!facilities.length){ status.textContent="施設が登録されていません。先に施設を登録してください。"; return; }
    if(p.accuracy > 150){ status.textContent=`GPS精度が低いため判定を保留しています（約${Math.round(p.accuracy)}m）。`; return; }
    const nearest=facilities.map(f=>({...f, distance:distance(p,f)})).sort((a,b)=>a.distance-b.distance)[0];
    const inside=nearest && nearest.distance <= nearest.radius_m;
    const now=Date.now();
    if(activeFacilityId){
      const active=facilities.find(f=>f.id===activeFacilityId);
      if(active && distance(p,active) <= active.radius_m + 50){
        outsideSince=null;
        if(now-lastHeartbeat>60000){ lastHeartbeat=now; postJson("/api/facilities/visit/heartbeat",{timestamp:localStamp()}).catch(e=>{status.textContent=e.message;}); }
        if(!inside || nearest.id!==activeFacilityId) status.textContent=`「${active.name}」の滞在を記録中（GPS誤差を考慮）。`;
        else status.textContent=`「${active.name}」の滞在を記録中。`;
        return;
      }
      if(outsideSince===null) outsideSince=now;
      if(now-outsideSince>=90000){ endVisit().catch(e=>status.textContent=e.message); }
      else status.textContent="登録施設の範囲外です。移動確認中…";
      return;
    }
    if(inside){
      if(insideCandidateId!==nearest.id){insideCandidateId=nearest.id;insideSince=now;}
      if(now-insideSince>=30000){ beginVisit(nearest).catch(e=>status.textContent=e.message); }
      else status.textContent=`「${nearest.name}」を検知候補にしています（確認中）。`;
    } else { insideCandidateId=null;insideSince=null;status.textContent="登録施設の近くにはいません。位置情報を確認中です。"; }
  }
  function stopTracking(finalize=true) {
    tracking=false;
    if(watchId!==null) navigator.geolocation.clearWatch(watchId);
    watchId=null; clearInterval(ticker);
    startBtn.disabled=false; stopBtn.disabled=true;
    if(finalize && activeFacilityId){ endVisit().catch(e=>status.textContent=e.message); }
    else status.textContent="停止中。位置情報の取得は終了しました。";
  }
  startBtn.addEventListener("click",()=>{
    if(!navigator.geolocation){status.textContent="このブラウザは位置情報に対応していません。";return;}
    if(!window.isSecureContext){status.textContent="位置情報にはHTTPS接続（またはlocalhost）が必要です。";return;}
    if(!facilities.length){status.textContent="先に施設を登録してください。";return;}
    tracking=true; startBtn.disabled=true;stopBtn.disabled=false;
    status.textContent="位置情報の許可を待っています…";
    updateActiveLabels(); ticker=setInterval(updateActiveLabels,1000);
    watchId=navigator.geolocation.watchPosition(processPosition,err=>{
      const msgs={1:"位置情報の利用が許可されませんでした。",2:"位置情報を取得できません。",3:"位置情報の取得がタイムアウトしました。"};
      status.textContent=msgs[err.code]||"位置情報の取得に失敗しました。"; stopTracking(true);
    },{enableHighAccuracy:true,maximumAge:5000,timeout:20000});
  });
  stopBtn.addEventListener("click",()=>stopTracking(true));
  // ページ離脱時は取得を終了し、滞在中なら離脱時刻で実績を確定する。
  window.addEventListener("pagehide",()=>{
    if(watchId!==null) navigator.geolocation.clearWatch(watchId);
    watchId=null; clearInterval(ticker);
    if(activeFacilityId){
      fetch("/api/facilities/visit/end",{method:"POST",headers:{"Content-Type":"application/json","X-CSRFToken":window.CSRF_TOKEN},body:JSON.stringify({timestamp:localStamp()}),keepalive:true}).catch(()=>{});
    }
  });
  updateActiveLabels();

  // Nominatim検索は利用者が検索ボタンを押した場合だけ行う。
  const searchBtn=byId("facility-search-button"), searchInput=byId("facility-search-query");
  const searchStatus=byId("facility-search-status"), resultsBox=byId("facility-search-results");
  if(searchBtn){
    searchBtn.addEventListener("click",async()=>{
      const q=searchInput.value.trim();
      if(q.length<2){searchStatus.textContent="施設名や住所を2文字以上入力してください。";return;}
      searchBtn.disabled=true;searchStatus.textContent="OpenStreetMapで検索しています…";resultsBox.replaceChildren();
      try{
        const r=await fetch(`/api/facilities/search?q=${encodeURIComponent(q)}`);
        const data=await r.json(); if(!r.ok||!data.ok) throw new Error(data.error||"検索に失敗しました。");
        if(!data.results.length){searchStatus.textContent="施設が見つかりませんでした。別の名称や住所で検索してください。";return;}
        searchStatus.textContent=`${data.results.length}件見つかりました。登録する施設を選択してください。`;
        data.results.forEach(item=>{
          const button=document.createElement("button");button.type="button";button.className="facility-result";
          const title=document.createElement("strong");title.textContent=item.name;
          const sub=document.createElement("span");sub.textContent=item.display_name;
          button.append(title,sub);
          button.addEventListener("click",()=>{
            byId("facility-name").value=item.name;
            byId("facility-latitude").value=item.latitude;
            byId("facility-longitude").value=item.longitude;
            searchStatus.textContent=`「${item.name}」を選択しました。活動と判定範囲を指定して登録してください。`;
            document.querySelector("#facility-add-form").scrollIntoView({behavior:"smooth",block:"center"});
          });
          resultsBox.append(button);
        });
      }catch(e){searchStatus.textContent=e.message;}finally{searchBtn.disabled=false;}
    });
  }
  const clearBtn=byId("clear-locations");
  clearBtn?.addEventListener("click",async()=>{
    if(!confirm("自分の位置情報記録をすべて削除します。この操作は取り消せません。"))return;
    try{const r=await fetch("/api/locations/clear",{method:"POST",headers:{"X-CSRFToken":window.CSRF_TOKEN}});if(r.ok)location.reload();else status.textContent="削除に失敗しました。";}catch(e){status.textContent=e.message;}
  });
})();

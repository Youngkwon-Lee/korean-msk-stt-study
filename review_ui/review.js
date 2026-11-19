'use strict';
const $ = id => document.getElementById(id);
const names = {unreviewed:'미검토',draft:'초안',reviewed:'완료',flagged:'보류'};
let session, current, version=0, dirty=false, inflight=null, timer=null, composing=false, sequence=0, storedPlayed=0, locked=false, confirmed=false, continuousStarted=false, playbackBlocked=false;
function error(message){$('error').textContent=message;$('error').hidden=!message;}
function status(message,failed=false){$('save-state').textContent=message;$('save-state').classList.toggle('failed',failed);}
async function api(path,options={}){
  const res=await fetch(path,{...options,headers:{...(options.body?{'Content-Type':'application/json','X-Review-Token':session.csrf_token}:{}),...options.headers}});
  const value=await res.json();if(!res.ok)throw new Error(value.error||'요청을 완료하지 못했습니다.');return value;
}
function played(){let seconds=0;const ranges=$('audio').played;for(let i=0;i<ranges.length;i++)seconds+=ranges.end(i)-ranges.start(i);return Math.max(storedPlayed,seconds);}
function controls(){
  const finished=Boolean(session&&current&&session.reviewed===session.total&&current.review?.status==='reviewed'&&!dirty);
  $('complete').disabled=locked||!current||finished||played()<.05||!$('transcript').value.trim();
  $('complete').textContent=finished?'전체 검토 완료':'확인 완료 · 다음';
  $('previous').disabled=locked||!current||current.ordinal===1;$('next').disabled=locked||!current||current.ordinal===session.total;
  $('flag').disabled=locked||!current;$('transcript').disabled=locked||!current;$('notes').disabled=locked||!current;
  $('play').disabled=locked||!current;$('edit').disabled=locked||!current;$('replay').disabled=locked||!current;
  $('play-label').textContent=!$('audio').paused?'일시정지':continuousStarted?'재생':'검토 시작';
  $('play').setAttribute('aria-pressed',String(!$('audio').paused));
  const hint=playbackBlocked?'자동 재생이 차단됐습니다. Space 또는 재생 버튼을 눌러 주세요.':played()<.05?'Space로 원음을 재생하세요.':'듣고 정답을 확인했으면 Enter로 완료합니다.';
  if($('listening-state').textContent!==hint)$('listening-state').textContent=hint;
}
function focusReview(){if(current)$('editor').focus({preventScroll:true});}
function focusEdit(){if(locked||!current)return;$('transcript').focus();}
async function startAudio(restart=false){
  if(!current)return;const id=current.id;continuousStarted=true;playbackBlocked=false;
  if(restart)$('audio').currentTime=0;
  try{await $('audio').play();}catch{if(current?.id===id)playbackBlocked=true;}
  if(current?.id===id)controls();
}
async function toggleAudio(){if(locked||!current)return;if($('audio').paused)await startAudio();else $('audio').pause();focusReview();}
function renderQueue(){
  $('reviewer').textContent=`검토자 · ${session.reviewer}`;$('progress').max=session.total;$('progress').value=session.reviewed;$('progress-label').textContent=`${session.reviewed} / ${session.total} 완료`;
  const filter=$('filter').value,list=$('queue');list.replaceChildren();
  for(const row of session.items){if(filter==='pending'&&row.status==='reviewed'||filter==='reviewed'&&row.status!=='reviewed'||filter==='flagged'&&row.status!=='flagged')continue;
    const button=document.createElement('button');button.type='button';button.className='queue-item';button.dataset.item=row.id;button.setAttribute('aria-label',`항목 ${row.id.slice(1)}, ${names[row.status]}`);
    if(current?.id===row.id)button.setAttribute('aria-current','page');
    const label=document.createElement('span');label.textContent=`항목 ${row.id.slice(1)}`;const state=document.createElement('span');state.className=`queue-status ${row.status}`;state.textContent=names[row.status];button.append(label,state);button.addEventListener('click',()=>navigate(row.id));list.append(button);
  }
}
function paintRecordState(record){$('item-status').textContent=names[record?.status||'unreviewed'];$('item-status').className=`badge ${record?.status||''}`;}
async function load(id){
  const item=await api(`/api/items/${id}`);$('audio').pause();current=item;version=item.review?.version||0;sequence=0;dirty=false;storedPlayed=item.review?.played_seconds||0;confirmed=item.review?.heard||false;playbackBlocked=false;
  $('item-title').textContent=`항목 ${item.id.slice(1)}`;$('position').textContent=`${item.ordinal} / ${session.total} · 원음과 제공 정답으로 검토합니다`;$('reference').textContent=item.reference;
  $('transcript').value=item.review?item.review.transcript:item.reference;$('notes').value=item.review?.notes||'';$('notes-details').open=Boolean(item.review?.status==='flagged');
  $('duration').textContent=`${item.duration.toFixed(2)}초`;$('audio').src=item.audio_url;$('audio').playbackRate=Number($('rate').value);$('editor').setAttribute('aria-busy','false');$('reload').hidden=true;
  paintRecordState(item.review);status(item.review?`저장됨 · ${new Date(item.review.saved_at).toLocaleTimeString('ko-KR')}`:'아직 저장된 검토가 없습니다');renderQueue();controls();
  if(continuousStarted&&$('autoplay').checked)await startAudio();
}
function edited(){if(!current||locked)return;dirty=true;confirmed=false;sequence++;$('reload').hidden=true;status('수정 중 · 잠시 후 초안 저장');controls();clearTimeout(timer);if(!composing)timer=setTimeout(()=>save('draft').catch(()=>{}),700);}
async function save(kind='draft'){
  clearTimeout(timer);if(inflight)await inflight;if(!current||kind==='draft'&&!dirty)return;
  const id=current.id,seq=sequence,payload={version,status:kind,transcript:$('transcript').value,notes:$('notes').value,heard:kind==='reviewed'||confirmed,played_seconds:played()};
  status('저장 중…');
  const operation=(async()=>{
    try{const result=await api(`/api/items/${id}`,{method:'POST',body:JSON.stringify(payload)});version=result.review.version;current.review=result.review;storedPlayed=result.review.played_seconds;session={...session,...result.summary};
      dirty=sequence!==seq;confirmed=dirty?false:result.review.heard;paintRecordState(result.review);renderQueue();error('');status(dirty?'수정 중 · 초안 저장 대기':`${kind==='draft'?'초안 저장됨':'저장됨'} · ${new Date(result.review.saved_at).toLocaleTimeString('ko-KR')}`);return result;
    }catch(e){dirty=true;status('저장하지 못했습니다 · 입력 내용 유지',true);$('reload').hidden=false;error(e.message);throw e;}
  })();inflight=operation;
  try{return await operation;}finally{inflight=null;controls();if(dirty&&$('reload').hidden&&!composing&&!locked)timer=setTimeout(()=>save('draft').catch(()=>{}),700);}
}
async function navigate(id){if(locked||current?.id===id)return;locked=true;controls();try{await save('draft');if(dirty)return;$('audio').pause();await load(id);$('queue-content').parentElement.classList.remove('open');$('queue-toggle').setAttribute('aria-expanded','false');}catch(e){error(e.message);}finally{locked=false;controls();if(!dirty)focusReview();}}
function nextPending(){const index=session.items.findIndex(x=>x.id===current.id);return [...session.items.slice(index+1),...session.items.slice(0,index)].find(x=>x.status!=='reviewed');}
async function finish(kind){if(locked||!current)return;if(kind==='reviewed'&&$('complete').disabled)return;locked=true;controls();
  try{await save(kind);$('audio').pause();const next=nextPending();if(next){await load(next.id);status('앞 항목 저장 완료 · 새 항목을 검토해 주세요');}else status(session.reviewed===session.total?'모든 항목을 저장했습니다. 검토 기록을 내보낼 수 있습니다.':'저장했습니다. 보류 항목을 확인해 주세요');}
  catch(e){error(e.message);}finally{locked=false;controls();if(!dirty)focusReview();}}
$('transcript').addEventListener('input',edited);$('notes').addEventListener('input',edited);
for(const id of ['transcript','notes']){$(id).addEventListener('compositionstart',()=>{composing=true;clearTimeout(timer);});$(id).addEventListener('compositionend',()=>{composing=false;edited();});}
for(const event of ['timeupdate','ended','play','pause','loadeddata'])$('audio').addEventListener(event,controls);
$('audio').addEventListener('play',()=>{continuousStarted=true;playbackBlocked=false;controls();});
$('audio').addEventListener('error',()=>error('음성을 불러오지 못했습니다. 로컬 서버와 원음을 확인해 주세요.'));
$('play').addEventListener('click',toggleAudio);$('edit').addEventListener('click',focusEdit);
$('replay').addEventListener('click',async()=>{if(!locked){await startAudio(true);focusReview();}});
$('rate').addEventListener('change',()=>{$('audio').playbackRate=Number($('rate').value);});
$('complete').addEventListener('click',()=>finish('reviewed'));$('flag').addEventListener('click',()=>finish('flagged'));
$('previous').addEventListener('click',()=>navigate(session.items[current.ordinal-2].id));$('next').addEventListener('click',()=>navigate(session.items[current.ordinal].id));$('filter').addEventListener('change',renderQueue);
$('reload').addEventListener('click',async()=>{if(confirm('현재 입력 내용을 버리고 저장된 검토를 다시 불러올까요?')){dirty=false;clearTimeout(timer);try{await load(current.id);error('');}catch(e){error(e.message);}}});
$('queue-toggle').addEventListener('click',()=>{const open=$('queue-content').parentElement.classList.toggle('open');$('queue-toggle').setAttribute('aria-expanded',String(open));});
$('export').addEventListener('click',async()=>{const button=$('export');button.disabled=true;try{await save('draft');if(dirty)return;const value=await api('/api/export');const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));const link=document.createElement('a');link.href=url;link.download=`reference-review-${session.reviewer}.json`;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);status('검토 기록을 JSON 파일로 내보냈습니다.');}catch(e){error(e.message);}finally{button.disabled=false;}});
window.addEventListener('beforeunload',e=>{if(dirty||inflight){e.preventDefault();e.returnValue='';}});
window.addEventListener('keydown',e=>{
  if(e.defaultPrevented||e.repeat||e.isComposing||composing||e.keyCode===229||locked)return;
  if((e.metaKey||e.ctrlKey)&&e.key==='Enter'){e.preventDefault();finish('reviewed');return;}
  if(e.metaKey||e.ctrlKey||e.altKey||e.shiftKey)return;
  // Preserve spaces/newlines, IME composition and native control activation.
  if(e.target.closest('input,textarea,select,button,a,summary,audio,[contenteditable]:not([contenteditable="false"])'))return;
  if(e.code==='Space'){e.preventDefault();toggleAudio();}
  else if(e.key==='Enter'){e.preventDefault();finish('reviewed');}
  else if(e.code==='KeyE'){e.preventDefault();focusEdit();}
  else if(e.code==='KeyR'){e.preventDefault();startAudio(true);}
});
(async()=>{try{session=await api('/api/session');renderQueue();$('export').disabled=false;await load((session.items.find(x=>x.status!=='reviewed')||session.items[0]).id);focusReview();}catch(e){error(e.message);status('연결을 확인해 주세요',true);}})();

// Synthetic DOM/HTTP boundaries exercise the actual browser script without dependencies.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const ui=path.join(__dirname,'../review_ui');
const source=fs.readFileSync(path.join(ui,'review.js'),'utf8');
const ids=[...fs.readFileSync(path.join(ui,'index.html'),'utf8').matchAll(/\bid="([^"]+)"/g)].map(x=>x[1]);
const settle=()=>new Promise(resolve=>setImmediate(resolve));

async function app(){
  const records=new Map(),requests=[],elements=new Map(),handlers=new Map(),timers=new Map();
  const state={failSave:false,blockPlay:false,plays:0,seconds:0};
  const document={activeElement:null,getElementById:id=>elements.get(id)||null,createElement:tag=>element(tag),body:null};
  function element(tag='div',id=''){
    const listeners=new Map();
    return {id,tagName:tag.toUpperCase(),disabled:false,hidden:false,checked:false,value:'',textContent:'',dataset:{},
      classList:{toggle(){},remove(){}},setAttribute(){},append(){},replaceChildren(){},remove(){},
      addEventListener(event,fn){if(!listeners.has(event))listeners.set(event,[]);listeners.get(event).push(fn);},
      async fire(event){await Promise.all((listeners.get(event)||[]).map(fn=>fn({target:this})));},
      focus(){document.activeElement=this;},
      closest(){return /^(INPUT|TEXTAREA|SELECT|BUTTON|A|SUMMARY|AUDIO)$/.test(this.tagName)?this:null;}};
  }
  for(const id of ids)elements.set(id,element(['transcript','notes'].includes(id)?'textarea':id==='audio'?'audio':id==='rate'||id==='filter'?'select':id==='autoplay'?'input':['play','edit','replay','complete','flag','previous','next','reload','export','queue-toggle'].includes(id)?'button':'div',id));
  document.body=element('body');elements.get('queue-content').parentElement=element();
  elements.get('rate').value='1';elements.get('filter').value='all';elements.get('autoplay').checked=true;
  const audio=elements.get('audio');audio.paused=true;audio.currentTime=0;
  audio.played={get length(){return state.seconds>0?1:0;},start(){return 0;},end(){return state.seconds;}};
  Object.defineProperty(audio,'src',{set(){state.seconds=0;audio.paused=true;}});
  audio.pause=()=>{audio.paused=true;audio.fire('pause');};
  audio.play=async()=>{if(state.blockPlay)throw Error('autoplay blocked');audio.paused=false;state.plays++;await audio.fire('play');};
  const rows=[{id:'R001',ordinal:1,reference:'합성 정답 하나',duration:.2},{id:'R002',ordinal:2,reference:'합성 정답 둘',duration:.2}];
  const summary=()=>({reviewer:'synthetic-reviewer',total:2,reviewed:[...records.values()].filter(r=>r.status==='reviewed').length,
    items:rows.map(r=>({id:r.id,status:records.get(r.id)?.status||'unreviewed'}))});
  const fetch=async(url,options={})=>{
    let value,status=200;
    if(url==='/api/session')value={...summary(),csrf_token:'synthetic-token'};
    else if(url.startsWith('/api/items/')){
      const id=url.split('/').at(-1);
      if(options.method==='POST'){
        const payload=JSON.parse(options.body);requests.push({id,...payload});
        if(state.failSave){state.failSave=false;value={error:'synthetic save conflict'};status=409;}
        else{const saved={...payload,version:payload.version+1,saved_at:'2026-01-01T00:00:00Z'};records.set(id,saved);value={review:saved,summary:summary()};}
      }else value={...rows.find(r=>r.id===id),audio_url:'/audio/'+id,review:records.get(id)||null};
    }else throw Error('unexpected request');
    return {ok:status===200,json:async()=>value};
  };
  const context=vm.createContext({document,fetch,console,URL,Blob,Date,
    setTimeout(fn){const id=timers.size+1;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);},
    window:{addEventListener(event,fn){handlers.set(event,fn);}},confirm:()=>false});
  vm.runInContext(source,context);await settle();
  const key=async(code,options={})=>{const e={code,key:code==='Space'?' ':code==='KeyE'?'e':code==='KeyR'?'r':code,
      target:document.activeElement||document.body,defaultPrevented:false,preventDefault(){this.defaultPrevented=true;},...options};
    handlers.get('keydown')(e);await settle();return e;};
  const listen=async()=>{state.seconds=.2;await audio.fire('timeupdate');};
  return {elements,document,state,records,requests,key,listen,context};
}

test('playback never confirms; Enter explicitly confirms once and auto-plays next',async()=>{
  const a=await app();assert.equal(a.state.plays,0);
  await a.key('Enter');assert.equal(a.requests.length,0);
  await a.key('Space');await a.listen();assert.equal(a.requests.length,0);
  await a.key('Enter');assert.equal(a.records.get('R001').heard,true);
  assert.equal(a.records.get('R001').status,'reviewed');assert.equal(a.records.get('R001').played_seconds,.2);
  assert.equal(a.state.plays,2);assert.equal(a.document.activeElement.id,'editor');
  await a.listen();await a.key('Enter',{repeat:true});assert.equal(a.records.has('R002'),false);
  await a.key('Enter');assert.equal(a.records.size,2);
  const count=a.requests.length;await a.key('Enter');assert.equal(a.requests.length,count);
  assert.equal(a.elements.get('complete').textContent,'전체 검토 완료');
});

test('editing preserves Space/Enter and IME; Cmd+Enter submits the corrected text',async()=>{
  const a=await app();await a.key('Space');await a.listen();await a.key('KeyE');
  assert.equal(a.document.activeElement.id,'transcript');
  for(const code of ['Space','Enter','KeyR','KeyE'])assert.equal((await a.key(code)).defaultPrevented,false);
  for(const flags of [{isComposing:true},{keyCode:229},{repeat:true}])await a.key('Enter',{metaKey:true,...flags});
  await a.elements.get('transcript').fire('compositionstart');await a.key('Enter',{ctrlKey:true});
  assert.equal(a.requests.length,0);
  a.elements.get('transcript').value='수정한 합성 정답';await a.elements.get('transcript').fire('compositionend');
  await a.key('Enter',{metaKey:true});assert.equal(a.records.get('R001').transcript,'수정한 합성 정답');
});

test('auto-play can be disabled and blocked playback never advances or confirms',async()=>{
  const a=await app();await a.key('Space');await a.listen();a.elements.get('autoplay').checked=false;
  await a.key('Enter');assert.equal(a.state.plays,1);assert.equal(a.records.has('R002'),false);
  const b=await app();b.state.blockPlay=true;await b.key('Space');
  assert.match(b.elements.get('listening-state').textContent,/Space/);
  await b.key('Enter');assert.equal(b.requests.length,0);
});

test('save failure keeps the item and edit, without next playback; explicit retry works',async()=>{
  const a=await app();await a.key('Space');await a.listen();
  a.elements.get('transcript').value='보존할 합성 수정';await a.elements.get('transcript').fire('input');a.state.failSave=true;
  await a.key('Enter');assert.equal(a.records.size,0);assert.equal(a.state.plays,1);
  assert.equal(a.elements.get('item-title').textContent,'항목 001');assert.equal(a.elements.get('transcript').value,'보존할 합성 수정');
  assert.equal(a.elements.get('error').hidden,false);
  await a.key('Enter');assert.equal(a.records.get('R001').transcript,'보존할 합성 수정');assert.equal(a.state.plays,2);
});

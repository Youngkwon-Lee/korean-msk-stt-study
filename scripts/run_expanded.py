#!/usr/bin/env python3
"""Expanded-corpus execution: concurrent API accuracy, batched Qwen, serial latency.

Accuracy and latency are separate runs. All receipts and references stay private.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import redirect_stdout
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from compare_api import api_settings, api_transcribe
from prepare_corpus import digest
from run_manifest import atomic_json, run_samples


def receipt_path(out, row):
    return Path(out)/(hashlib.sha256(row['sample_id'].encode()).hexdigest()+'.json')


def pending_rows(rows, out, config):
    out=Path(out);out.mkdir(mode=0o700,parents=True,exist_ok=True)
    receipt=out/'run.json'
    if receipt.exists():
        if json.loads(receipt.read_text())!=config:
            raise ValueError('Changed settings require a new run')
    else:
        atomic_json(receipt,config)
    pending=[]
    for row in rows:
        p=receipt_path(out,row)
        if p.exists():
            previous=json.loads(p.read_text())
            if previous['sample_id']!=row['sample_id'] or previous['audio_sha256']!=row['audio_sha256']:
                raise ValueError('Receipt identity mismatch')
            if previous['status'] not in ('success','failed'):
                raise RuntimeError('Ambiguous request requires reconciliation')
        else:
            pending.append(row)
    return pending


def progress(out, rows, done, status='running'):
    atomic_json(Path(out)/'progress.json',{'status':status,'completed_this_invocation':done,
        'planned_samples':len(rows),'checked_at':datetime.now(timezone.utc).isoformat()})


def phase_api(rows, out, config, transcribe, workers):
    pending=pending_rows(rows,out,config)
    done=0
    with redirect_stdout(io.StringIO()):
        pool=ThreadPoolExecutor(max_workers=workers)
        futures=[pool.submit(run_samples,[row],out,config,transcribe) for row in pending]
        try:
            for future in as_completed(futures):
                future.result();done+=1
                if done%25==0:progress(out,rows,done)
        finally:
            pool.shutdown(wait=True,cancel_futures=True)
    progress(out,rows,done,'complete')


def phase_batches(rows,out,config,transcribe_batch,batch_size):
    pending=pending_rows(rows,out,config)
    done=0
    for offset in range(0,len(pending),batch_size):
        batch=pending[offset:offset+batch_size]
        batch_id=hashlib.sha256((':'.join(r['sample_id'] for r in batch)).encode()).hexdigest()
        started=datetime.now(timezone.utc).isoformat()
        for row in batch:
            atomic_json(receipt_path(out,row),{'sample_id':row['sample_id'],'audio_sha256':row['audio_sha256'],
                'status':'in_flight','attempts':1,'batch_id':batch_id,'started_at':started})
        tick=time.monotonic()
        try:
            values=transcribe_batch([Path(r['audio_path']) for r in batch])
            if len(values)!=len(batch) or not all(isinstance(v,str) for v in values):
                raise ValueError('Invalid backend cardinality')
        except Exception:
            for row in batch:
                atomic_json(receipt_path(out,row),{'sample_id':row['sample_id'],'audio_sha256':row['audio_sha256'],
                    'status':'failed','attempts':1,'batch_id':batch_id,'error_code':'BATCH_FAILED'})
            raise RuntimeError('BATCH_FAILED') from None
        elapsed=time.monotonic()-tick
        for row,text in zip(batch,values):
            atomic_json(receipt_path(out,row),{'sample_id':row['sample_id'],'audio_sha256':row['audio_sha256'],
                'status':'success','attempts':1,'started_at':started,'hypothesis':text,
                'batch_id':batch_id,'batch_seconds':elapsed,'batch_size':len(batch),
                'http_seconds':None,'processing_seconds':elapsed if batch_size==1 else None,
                'timing_scope':'local decode and inference batch; not HTTP',
                'response_model':'Qwen3-ASR-1.7B'})
        done+=len(batch)
        if done%20==0:progress(out,rows,done)
    progress(out,rows,done,'complete')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('manifest','expected-manifest-sha256','audio-root','private-out','annotations'):
        p.add_argument('--'+name,required=True)
    p.add_argument('--provider',choices=['gpt-transcribe','qwen-local'],required=True)
    p.add_argument('--external-audio-use-authorized',action='store_true')
    p.add_argument('--workers',type=int,default=4)
    p.add_argument('--batch-size',type=int,default=4)
    p.add_argument('--service-source')
    p.add_argument('--qwen-model')
    p.add_argument('--gpu',type=int,default=1)
    p.add_argument('--lock')
    args=p.parse_args()
    if not 1<=args.workers<=4 or not 1<=args.batch_size<=4:p.error('Concurrency outside declared limit')
    os.umask(0o077)
    raw=Path(args.manifest).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=args.expected_manifest_sha256:p.error('Manifest mismatch')
    manifest=json.loads(raw)
    if not manifest['summary']['run_ready_audio_hashes']:p.error('Audio not sealed')
    rows=manifest['samples']
    if len({r['sample_id'] for r in rows})!=len(rows):p.error('Duplicate identity')
    for row in rows:
        local=Path(args.audio_root)/(row['sample_id']+'.wav')
        if digest(local)!=row['audio_sha256']:p.error('Input bytes differ')
        row['audio_path']=str(local)
        if row['audio_seconds']>30:p.error('Longer clip requires predeclared chunking')
    latency=[r for r in rows if r['latency_substudy']]
    out=Path(args.private_out);out.mkdir(mode=0o700,parents=True,exist_ok=True)
    config={'schema':'manifest-run/v1','manifest_sha256':args.expected_manifest_sha256,
        'provider':args.provider,'condition_id':'expanded-v0.2a',
        'annotation_sha256':digest(args.annotations),'automatic_retries':0,
        'source_sha256':{n:digest(Path(__file__).parent/n) for n in ('run_expanded.py','run_manifest.py','compare_api.py','metrics.py')}}
    with (out/'controller.lock').open('a') as controller:
        fcntl.flock(controller,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.provider=='gpt-transcribe':
            key=os.environ.get('OPENAI_API_KEY','')
            if not key or not args.external_audio_use_authorized:p.error('Approved key and expanded-use authorization required')
            transcribe=lambda path:api_transcribe(path,key,'gpt-transcribe')
            config.update(api_settings=api_settings('gpt-transcribe'),accuracy_workers=args.workers)
            phase_api(rows,out/'accuracy',{**config,'phase':'accuracy'},transcribe,args.workers)
            phase_api(latency,out/'latency',{**config,'phase':'latency','workers':1},transcribe,1)
        else:
            if not all((args.service_source,args.qwen_model,args.lock)):p.error('Existing Qwen source/model/lease required')
            os.environ['CUDA_VISIBLE_DEVICES']=str(args.gpu)
            with open(args.lock,'r+') as lease:
                fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
                state=subprocess.check_output(['nvidia-smi','--id='+str(args.gpu),'--query-gpu=memory.used,utilization.gpu','--format=csv,noheader,nounits'],text=True).strip()
                used,util=map(int,state.split(','))
                if used>2048 or util>10:p.error('GPU is occupied')
                sys.path.insert(0,args.service_source)
                import torch
                import numpy as np
                from qwen_asr import Qwen3ASRModel
                from kinelo_stt.inference import decode
                started=time.monotonic()
                model=Qwen3ASRModel.from_pretrained(args.qwen_model,dtype=torch.float16,device_map='cuda:0',
                    attn_implementation='eager',max_inference_batch_size=args.batch_size,
                    max_new_tokens=1024,local_files_only=True)
                torch.cuda.synchronize();load_seconds=time.monotonic()-started
                with torch.inference_mode():model.transcribe(audio=(np.zeros(32000,dtype=np.float32),16000),language='Korean')
                torch.cuda.synchronize()
                def transcribe_batch(paths):
                    audio=[(decode(path,180),16000) for path in paths]
                    with torch.inference_mode():values=model.transcribe(audio=audio,language='Korean')
                    torch.cuda.synchronize()
                    return [r.text.strip() for r in values]
                config.update(accuracy_batch_size=args.batch_size,precision='float16',attention='eager',
                    max_new_tokens=1024,language='Korean',model_load_seconds=load_seconds,
                    package_versions={n:__import__(n).__version__ for n in ('torch','transformers','numpy','av')},
                    model_file_sha256={f.name:digest(f) for f in sorted(Path(args.qwen_model).iterdir())
                        if f.is_file() and (f.suffix=='.safetensors' or f.name=='config.json')},
                    gpu_device=subprocess.check_output(['nvidia-smi','--id='+str(args.gpu),
                        '--query-gpu=name,driver_version','--format=csv,noheader'],text=True).strip(),
                    decode_source_sha256=digest(Path(args.service_source)/'kinelo_stt/inference.py'))
                # Keep nondeterministic timing outside resume identity.
                timing={'model_load_seconds':config.pop('model_load_seconds'),'gpu_initial':state}
                atomic_json(out/'initialization.json',timing)
                phase_batches(rows,out/'accuracy',{**config,'phase':'accuracy'},transcribe_batch,args.batch_size)
                phase_batches(latency,out/'latency',{**config,'phase':'latency','effective_batch_size':1},transcribe_batch,1)
    atomic_json(out/'complete.json',{'status':'complete','manifest_sha256':args.expected_manifest_sha256,
        'accuracy_samples':len(rows),'latency_samples':len(latency),'finished_at':datetime.now(timezone.utc).isoformat()})
    print('Expanded accuracy and serial latency phases complete.')


if __name__=='__main__':main()

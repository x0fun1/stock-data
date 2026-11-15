"""Deterministic title -> Search/Fetch -> evidence table, with no LLM calls."""
from pathlib import Path
from urllib.parse import urlsplit
import hashlib
import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from ..common.hashing import canonical_hash, sha256_file
from ..common.jsonio import read_json, write_json
from ..common.timeutil import now_utc


class TinyFishSearchFetch:
    name='tinyfish_search_fetch_rest'

    def __init__(self, api_key=None, session=None):
        self._api_key=api_key or os.environ.get('TINYFISH_API_KEY')
        if not self._api_key:
            raise ValueError('TinyFish Search/Fetch requires TINYFISH_API_KEY in the local environment')
        self.session=session or requests.Session()
        self._last_search=0.
        self._last_fetch=0.

    def _request(self, method, url, **kwargs):
        try:
            response=self.session.request(method,url,headers={'X-API-Key':self._api_key},
                timeout=50,verify=True,**kwargs)
        except requests.RequestException as error:
            return {'status':'unavailable','reason':'network:'+type(error).__name__}
        if response.status_code in (401,402):
            raise RuntimeError('TinyFish authentication or free allowance unavailable; batch stopped without automatic top-up')
        if response.status_code>=400:
            return {'status':'unavailable','reason':f'http_{response.status_code}'}
        try:
            data=response.json()
        except ValueError:
            return {'status':'unavailable','reason':'provider_response_not_json'}
        return {'status':'ok','data':data}

    def search(self, task, query):
        wait=2.05-(time.monotonic()-self._last_search)
        if wait>0:time.sleep(wait)
        self._last_search=time.monotonic()
        response=self._request('GET','https://api.search.tinyfish.ai',
            params={'query':query,'location':'US','language':'en','domain_type':'news'})
        if response['status']!='ok':return {**response,'sources':[]}
        sources=response['data'].get('results',[])
        if not isinstance(sources,list):raise ValueError('TinyFish search results must be a list')
        return {'status':'ok','sources':[{'url':row['url'],'title':row.get('title',''),
            'published_at':row.get('date'),'snippet':row.get('snippet','')} for row in sources if row.get('url')]}

    def fetch(self, task, urls):
        wait=.41*len(urls)-(time.monotonic()-self._last_fetch)
        if wait>0:time.sleep(wait)
        self._last_fetch=time.monotonic()
        response=self._request('POST','https://api.fetch.tinyfish.ai',
            json={'urls':urls,'format':'markdown','per_url_timeout_ms':45000})
        if response['status']!='ok':return [{'url':url,**response} for url in urls]
        data=response['data']
        results=[{'url':row['url'],'title':row.get('title'), 'published_at':row.get('published_date'),
            'markdown':row.get('text') or '', 'status':'fetched','fetched_at':now_utc().isoformat()}
            for row in data.get('results',[]) if row.get('url') in urls]
        known={row['url'] for row in results}
        errors={row.get('url'):row.get('code',row.get('error','provider_fetch_failed')) for row in data.get('errors',[])}
        results.extend({'url':url,'status':'unavailable','reason':errors.get(url,'missing_provider_result')} for url in urls if url not in known)
        return results


class ArchivedSearchFetch:
    """Replay retained search/fetch evidence; never label it as a new search."""
    name='archived_search_fetch_replay'

    def __init__(self, folder):self.folder=Path(folder)

    def search(self, task, query):
        path=self.folder/'searches'/f"{task['task_id']}.json"
        if not path.exists():return {'status':'unavailable','reason':'archived_search_missing','sources':[]}
        data=read_json(path)
        return {'status':'ok','sources':data.get('sources',[]),'archived_query':data.get('query'),
            'archived_at':data.get('searched_at'),'source_record_sha256':sha256_file(path)}

    def fetch(self, task, urls):
        path=self.folder/'reader-evidence'/f"{task['task_id']}.json"
        if not path.exists():return [{'url':url,'status':'unavailable','reason':'archived_fetch_missing'} for url in urls]
        data=read_json(path);readers=data.get('readers',[])
        output=[]
        for url in urls:
            row=next((row for row in readers if row.get('url')==url),None)
            if row is None:output.append({'url':url,'status':'unavailable','reason':'URL_not_in_archived_fetch'})
            else:output.append({**row,'status':'fetched' if row['status']=='read' else 'unavailable',
                'markdown':row.get('brief_quote',''),'excerpt_only':True,
                'archived_at':row.get('read_at'),'source_record_sha256':sha256_file(path)})
        return output


class ConnectedPluginBatch:
    """Consume a live batch made by the authenticated host tool runner."""
    name='tinyfish_connected_plugin_batch'

    def __init__(self, folder):
        self.folder=Path(folder)
        if (self.folder/'invalid-driver.json').exists():
            raise ValueError('invalid driver batch cannot be used as evidence')
        manifest=read_json(self.folder/'manifest.json')
        result=read_json(self.folder/'driver-result.json')
        if manifest.get('provider')!='tinyfish_connected_plugin' or not manifest.get('connected_authorization'):
            raise ValueError('authenticated TinyFish plugin provenance required')
        if not result.get('all_collected') or result.get('fatal_error'):
            raise ValueError('plugin batch still incomplete')
        if len(list((self.folder/'items').glob('*.json')))!=result['total']:
            raise ValueError('plugin batch item coverage disagrees with completion')

    def _item(self,task):
        path=self.folder/'items'/f"{task['task_id']}.json"
        item=read_json(path)
        if item.get('task_id')!=task['task_id'] or item.get('title')!=task['title']:
            raise ValueError('plugin batch item disagrees with original title')
        return item,path

    def search(self,task,query):
        item,path=self._item(task)
        match=next((attempt for attempt in item.get('searches',[]) if attempt['query']==query),None)
        if match is None:
            return {'status':'unavailable','sources':[],'reason':'query_not_present_in_live_plugin_batch',
                'source_record_sha256':sha256_file(path)}
        return {**match['result'],'native_query':match['query'],'native_searched_at':match['searched_at'],
            'response_sha256':match.get('response_sha256'),'source_record_sha256':sha256_file(path),
            'collection_origin':'connected_plugin_live'}

    def fetch(self,task,urls):
        item,path=self._item(task)
        output=[]
        for url in urls:
            row=next((row for row in item.get('documents',[]) if row.get('url')==url),None)
            if row is None:
                output.append({'url':url,'status':'unavailable','reason':'URL_not_fetched_in_live_plugin_batch'})
            else:
                output.append({**row,'markdown':row.get('excerpt',''),'excerpt_only':True,
                    'source_record_sha256':sha256_file(path),'collection_origin':'connected_plugin_live'})
        return output


def _public_url(value):
    if not isinstance(value,str):return False
    try:parts=urlsplit(value)
    except ValueError:return False
    if parts.scheme!='https' or not parts.hostname or parts.hostname in {'localhost','127.0.0.1','::1'}:
        return False
    domain=parts.hostname.lower()
    # Quote hubs can contain matching headlines, but are not the source article.
    return not (('finance.yahoo.com' in domain and parts.path.startswith(('/quote/','/chart/')))
        or (domain.endswith('seekingalpha.com') and parts.path.startswith('/symbol/'))
        or (domain.endswith('barrons.com') and parts.path.startswith('/market-data/'))
        or (domain.endswith('nasdaq.com') and parts.path.startswith('/market-activity/')))


def _publication_metadata(value):
    """Normalize explicit zones; a date alone is not silently treated as UTC."""
    if not value:return None
    try:
        stamp=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    except ValueError:
        try:stamp=parsedate_to_datetime(str(value))
        except (ValueError,TypeError,IndexError):return None
    return stamp.astimezone(timezone.utc).isoformat() if stamp.tzinfo else None


def _collect_task(task, provider, max_sources):
    searches=[];documents=[];seen=set()
    queries=[task['title'],f"{task['ticker']} {task['title']} {(task.get('published_at') or '')[:10]}"]
    def fetch_candidates(candidates):
        urls=[source['url'] for source in candidates]
        if urls:
            for original in provider.fetch(task,urls):
                row=dict(original)
                body=row.pop('markdown','')
                raw_publication=row.get('published_at')
                source=next((source for source in candidates if source['url']==row.get('url')), {})
                row['fetch_publication_raw']=raw_publication
                row['search_publication_raw']=source.get('published_at')
                row['published_at']=_publication_metadata(raw_publication)
                row['publication_time_basis']='fetch_metadata' if row['published_at'] else 'unavailable_or_timezone_unknown'
                if not row['published_at'] and _publication_metadata(source.get('published_at')):
                    row['published_at']=_publication_metadata(source['published_at'])
                    row['publication_time_basis']='search_metadata'
                if body:
                    row['content_sha256']=row.get('content_sha256') or hashlib.sha256(body.encode()).hexdigest()
                    row['excerpt']=' '.join(body.split()[:200])
                    row['excerpt_truncated']=bool(row.get('excerpt_only')) or len(body.split())>200
                restricted=any(term in body[:4000].lower() for term in (
                    'access denied','verify you are human','enable javascript and cookies',
                    'subscribe to continue','sign in to read','we do not host the original article'))
                if row.get('status')=='fetched' and (not row.get('excerpt') or restricted):
                    row.update(status='unavailable',reason='access_restricted' if restricted else 'empty_body_or_excerpt')
                # HTTP fetch is evidence acquisition, not semantic verification.
                row['semantic_review_complete']=False
                documents.append(row)
    if getattr(provider,'prefer_source_url',False) and _public_url(task.get('source_url')):
        url=task['source_url'];seen.add(url)
        # Finnhub's timestamp remains discovery context, not the publisher's date.
        searches.append({'kind':'provided_source_url','query':None,'searched_at':None,
            'result':{'status':'not_searched','sources':[{'url':url}]}})
        fetch_candidates([{'url':url}])
    for index,query in enumerate(queries):
        if any(row['status']=='fetched' for row in documents):break
        # Archive replay cannot invent a thematic search that was never made.
        if index and isinstance(provider,ArchivedSearchFetch):break
        search=provider.search(task,query)
        searches.append({'query':query,'kind':'exact_title' if index==0 else 'company_event_date',
            'searched_at':now_utc().isoformat(),'result':search})
        candidates=[]
        for source in search.get('sources',[]):
            url=source.get('url','')
            if _public_url(url) and url not in seen:
                candidates.append(source);seen.add(url)
                if len(candidates)>=max_sources:break
        fetch_candidates(candidates)
        if any(row['status']=='fetched' for row in documents):break
    return {'task_id':task['task_id'],'title':task['title'],'source_published_at':task.get('published_at'),
        'provider':provider.name,'searches':searches,'documents':documents,
        'status':'fetched_for_review' if any(row['status']=='fetched' for row in documents) else 'fetch_unresolved',
        'llm_called':False,'sentiment_assessed':False}


def render_collection_table(records):
    def cell(value):return str(value or '—').replace('|','／').replace('\n',' ').replace('<','&lt;').replace('>','&gt;')
    lines=['# 新闻采集证据表（分析前）','',
        '本表由程序逐标题搜索/抓取后生成，不包含利多利空判断。正文截段、转载日期及公司关联需在下一阶段核对；抓取成功不等于事实已验证。', '',
        '| 原标题 | 采集状态 | 抓取来源 | 来源发布日期 | 正文截段／访问缺口 | 内容哈希 |',
        '|---|---|---|---|---|---|']
    for record in records:
        documents=record['documents'] or [{'reason':'no_candidate_source'}]
        for row in documents:
            url=row.get('url','');source=f"[{cell(row.get('title') or url)}]({url})" if url else '无来源'
            excerpt=row.get('excerpt') or row.get('reason','body_missing')
            lines.append(f"| {cell(record['title'])} | {cell(row.get('status',record['status']))} | {source} | {cell(row.get('published_at'))} | {cell(excerpt)} | {cell(row.get('content_sha256'))} |")
    return '\n'.join(lines)+'\n'


def collect_news_evidence(runtime_root, workflow_id, collection_id, provider='local', max_sources=2, client=None,
                          plugin_batch_id=None, workers=None, search_url=None, fetch_url=None, local_timeout=45):
    root=Path(runtime_root)
    source=root/'news-workflows'/workflow_id
    folder=source/'collections'/collection_id
    if not folder.resolve().is_relative_to(root.resolve()):raise ValueError('collection must remain inside runtime')
    tasks=read_json(source/'tasks.json')
    if client is None:
        if provider=='tinyfish-plugin':
            if not plugin_batch_id:raise ValueError('tinyfish-plugin requires a completed plugin batch ID')
            batch=source/'plugin-batches'/plugin_batch_id
            if not batch.resolve().is_relative_to(root.resolve()):raise ValueError('plugin batch leaves runtime')
            client=ConnectedPluginBatch(batch)
        elif provider=='archive':client=ArchivedSearchFetch(source)
        elif provider=='tinyfish':client=TinyFishSearchFetch()
        elif provider=='local':
            from .local_search_fetch import LocalSearchFetch
            client=LocalSearchFetch(search_url,fetch_url,local_timeout)
        else:raise ValueError('unknown Search/Fetch provider')
    if workers is None:workers=32 if getattr(client,'supports_concurrency',False) else 1
    if not 1<=max_sources<=5:raise ValueError('max_sources must be between 1 and 5')
    if not 1<=workers<=32:raise ValueError('workers must be between 1 and 32')
    if workers>1 and not getattr(client,'supports_concurrency',False):
        raise ValueError('parallel collection requires a concurrency-safe provider; use local or workers=1')
    manifest={'workflow_id':workflow_id,'collection_id':collection_id,'provider':client.name,
        'title_tasks_sha256':canonical_hash([{'task_id':task['task_id'],'title':task['title']} for task in tasks]),
        'max_sources':max_sources,'llm_in_collection':False,'cleanup_enabled':False,'evidence_scope':'source_excerpt'}
    if isinstance(client,ConnectedPluginBatch):
        manifest['plugin_batch_id']=plugin_batch_id
    if hasattr(client,'collection_config'):
        manifest['local_config']=client.collection_config
        manifest['title_tasks_sha256']=canonical_hash([{key:task.get(key) for key in
            ('task_id','title','ticker','published_at','source_url')} for task in tasks])
    path=folder/'manifest.json'
    if path.exists() and read_json(path)!=manifest:raise ValueError('collection protocol changed; use a new collection ID')
    write_json(path,manifest)
    records_by_id={};pending=[]
    completion_path=folder/'completion.json'
    old_completion=read_json(completion_path) if completion_path.exists() else None
    for task in tasks:
        record_path=folder/'items'/f"{task['task_id']}.json"
        if record_path.exists():
            record=read_json(record_path)
            if record.get('task_id')!=task['task_id'] or record.get('title')!=task['title']:
                raise ValueError('cached collection item disagrees with frozen title')
            records_by_id[task['task_id']]=record
        else:
            if old_completion:raise ValueError('completed collection item missing; use a new collection ID')
            pending.append(task)
    start=time.monotonic()
    if pending and hasattr(client,'check_available'):client.check_available()
    def collect_and_save(task):
        frozen={key:task.get(key) for key in ('task_id','title','ticker','published_at','source_url','market')}
        record=_collect_task(frozen,client,max_sources)
        write_json(folder/'items'/f"{task['task_id']}.json",record)
        return record
    if workers==1:
        for task in pending:
            record=collect_and_save(task);records_by_id[record['task_id']]=record
    else:
        with ThreadPoolExecutor(max_workers=workers,thread_name_prefix='news-collect') as pool:
            futures=[pool.submit(collect_and_save,task) for task in pending]
            for future in as_completed(futures):
                record=future.result();records_by_id[record['task_id']]=record
    records=[records_by_id[task['task_id']] for task in tasks]
    if old_completion and canonical_hash(records)!=old_completion['records_sha256']:
        raise ValueError('completed collection evidence changed')
    if old_completion:return old_completion
    output={'total_titles':len(records),'fetched_for_review':sum(row['status']=='fetched_for_review' for row in records),
        'fetch_unresolved':sum(row['status']=='fetch_unresolved' for row in records),'llm_called':False,
        'is_live_provider_run':not isinstance(client,ArchivedSearchFetch),'cleanup_performed':False,
        'python_network_calls':not isinstance(client,(ArchivedSearchFetch,ConnectedPluginBatch)),
        'records_sha256':canonical_hash(records),'markdown':str(folder/'evidence-table.md'),
        'workers':workers,'elapsed_seconds':round(time.monotonic()-start,3),
        'newly_collected_titles':len(pending),
        'search_requests':sum(attempt.get('kind')!='provided_source_url' for record in records for attempt in record['searches'])}
    write_json(folder/'collection.json',records)
    (folder/'evidence-table.md').write_text(render_collection_table(records),encoding='utf-8')
    write_json(folder/'completion.json',output)
    return output


def prepare_plugin_title_plan(runtime_root,workflow_id,batch_id):
    """Expose only frozen title requests, never prior LLM sentiment decisions."""
    root=Path(runtime_root).resolve()
    source=root/'news-workflows'/workflow_id
    folder=source/'plugin-batches'/batch_id
    if not folder.resolve().is_relative_to(root):raise ValueError('plugin batch leaves runtime')
    tasks=read_json(source/'tasks.json')
    plan={'workflow_id':workflow_id,'batch_id':batch_id,'folder':str(folder),
        'tasks':[{key:task.get(key) for key in ('task_id','title','ticker','published_at')} for task in tasks],
        'source_title_count':len(tasks),'llm_in_collection':False,
        'driver_reference':'references/tinyfish-plugin-batch.md',
        'search_tool':'TinyFish search','fetch_tool':'TinyFish fetch_content'}
    path=folder/'title-plan.json'
    if path.exists() and read_json(path)!=plan:
        raise ValueError('frozen plugin title plan changed; choose a new batch ID')
    write_json(path,plan)
    manifest_path=folder/'manifest.json'
    if not manifest_path.exists():
        write_json(manifest_path,{'workflow_id':workflow_id,'batch_id':batch_id,
            'provider':'tinyfish_connected_plugin','source_title_count':len(tasks),
            'connected_authorization':False,'cleanup_enabled':False})
    return {**plan,'manifest':read_json(manifest_path),
        'completed_ids':[path.stem for path in (folder/'items').glob('*.json')]}

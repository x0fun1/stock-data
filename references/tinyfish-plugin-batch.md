# 已连接TinyFish的批量驱动

旧批次适用[七天缓存清理](cache-retention.md)。驱动中的 cleanup 标记仅说明当前批次执行期间不删除证据；独立的运行前清理仍会删除超过七天的旧批次。

在支持tools对象的宿主工具运行器中执行一次本驱动。标题队列与运行目录由tinyfish_tasks、tinyfish_native_config提供；普通Node进程没有自动获得聊天插件OAuth连接。整个逐标题Search/Fetch循环由程序运行，不由大语言模型逐条选择或发起请求。保留源标题、查询、内容SHA256、截段和访问缺口；不计算情绪。行情/新闻目录URL先按固定规则排除，再选择原始文章URL；原文抓取成功不等于标题、公司与日期已核实。最大8个工作线程，Search全局间隔2.5秒，每次Fetch最多2个URL。批次目录已完成IDs可用于断点继续；保留旧批次，用新batch_id重新采集。

先用`news-plugin-plan`输出计划；宿主将计划中的tasks保存为tinyfish_tasks，将folder、manifest、completed_ids、workflow_id、batch_id保存为tinyfish_native_config，再执行下面程序。程序在实际工具响应后确认鉴权，并保存批次manifest/driver-result；完成后用`news-collect --provider tinyfish-plugin --plugin-batch-id ...`生成表格。

驱动为当前已连接工具接口版本，其他宿主需绑定相应search/fetch方法。此段只展示程序逻辑，不会因读取文档自动调用网络。

```javascript
async function runTinyFishBatch(){
  const cfg=load("tinyfish_native_config");
  const tasks=load("tinyfish_tasks");
  if(!cfg || !tasks)throw new Error("Missing native batch configuration/title queue");
  function sha256(input){
    const bytes=[];
    for(const char of input){
      let cp=char.codePointAt(0);
      if(cp>=0xd800&&cp<=0xdfff)cp=0xfffd;
      if(cp<0x80)bytes.push(cp);
      else if(cp<0x800)bytes.push(0xc0|(cp>>6),0x80|(cp&63));
      else if(cp<0x10000)bytes.push(0xe0|(cp>>12),0x80|((cp>>6)&63),0x80|(cp&63));
      else bytes.push(0xf0|(cp>>18),0x80|((cp>>12)&63),0x80|((cp>>6)&63),0x80|(cp&63));
    }
    const length=bytes.length*8;
    bytes.push(0x80);while(bytes.length%64!==56)bytes.push(0);
    const high=Math.floor(length/4294967296),low=length>>>0;
    for(let i=3;i>=0;i--)bytes.push((high>>>(i*8))&255);
    for(let i=3;i>=0;i--)bytes.push((low>>>(i*8))&255);
    const K=[0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2];
    const h=[0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19];
    const rr=(x,n)=>(x>>>n)|(x<<(32-n));
    for(let off=0;off<bytes.length;off+=64){
      const w=new Array(64);
      for(let i=0;i<16;i++)w[i]=((bytes[off+4*i]<<24)|(bytes[off+4*i+1]<<16)|(bytes[off+4*i+2]<<8)|bytes[off+4*i+3])>>>0;
      for(let i=16;i<64;i++){const s0=rr(w[i-15],7)^rr(w[i-15],18)^(w[i-15]>>>3),s1=rr(w[i-2],17)^rr(w[i-2],19)^(w[i-2]>>>10);w[i]=(w[i-16]+s0+w[i-7]+s1)>>>0;}
      let[a,b,c,d,e,f,g,k]=h;
      for(let i=0;i<64;i++){const t1=(k+(rr(e,6)^rr(e,11)^rr(e,25))+((e&f)^((~e)&g))+K[i]+w[i])>>>0,t2=((rr(a,2)^rr(a,13)^rr(a,22))+((a&b)^(a&c)^(b&c)))>>>0;k=g;g=f;f=e;e=(d+t1)>>>0;d=c;c=b;b=a;a=(t1+t2)>>>0;}
      [a,b,c,d,e,f,g,k].forEach((v,i)=>h[i]=(h[i]+v)>>>0);
    }
    return h.map(x=>x.toString(16).padStart(8,"0")).join("");
  }
  if(sha256("abc")!=="ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")throw new Error("Hash self-check failed");
  function parseResult(result){
    const pieces=(result.content??[]).filter(x=>x.type==="text").map(x=>x.text);
    const raw=pieces.join("\n");
    if(result.isError && /401|402|unauthor|oauth|insufficient.credit|free.allowance/i.test(raw))throw new Error("FATAL_AUTH_OR_ALLOWANCE");
    let data=result.structuredContent;
    if(!data || (!Array.isArray(data.results)&&!Array.isArray(data.errors))){
      try{data=JSON.parse(raw);}catch{data={results:[],errors:[{error:result.isError?"tool_error":"non_json_response"}]};}
    }
    return {data,sha:sha256(raw),isError:!!result.isError};
  }
  function publicUrl(url){return typeof url==="string" && /^https:\/\/[^/\s?#]+[^\s]*$/i.test(url) && !/^https:\/\/(?:localhost|127\.0\.0\.1|\[::1\])(?:[:/?#]|$)/i.test(url) && !/(?:finance\.yahoo\.com\/(?:quote|chart)\/|seekingalpha\.com\/symbol\/|barrons\.com\/market-data\/|nasdaq\.com\/market-activity\/)/i.test(url);}
  async function persist(path,value){
    const body=JSON.stringify(value,null,2);
    const patch="*** Begin Patch\n*** Add File: "+path+"\n"+body.split("\n").map(line=>"+"+line).join("\n")+"\n*** End Patch";
    await tools.apply_patch(patch);
  }
  function documentsFrom(parsed,urls){
    const output=[];
    for(const row of parsed.data.results??[]){
      if(!urls.includes(row.url))continue;
      const body=typeof row.text==="string"?row.text:"";
      const restricted=/access denied|verify you are human|enable javascript and cookies|subscribe to continue|sign in to read/i.test(body.slice(0,4000));
      const words=body.trim().split(/\s+/).filter(Boolean);
      const usable=body.length>=300&&!restricted;
      output.push({url:row.url,final_url:row.final_url,title:row.title,author:row.author,published_at:row.published_date,
        status:usable?"fetched":"unavailable",reason:usable?null:restricted?"access_restricted":"body_too_short",
        excerpt:usable?words.slice(0,200).join(" "):"",excerpt_truncated:usable&&words.length>200,
        content_word_count:words.length,content_sha256:sha256(body),response_sha256:parsed.sha,
        fetched_at:new Date().toISOString(),semantic_review_complete:false});
    }
    const known=new Set(output.map(x=>x.url));
    for(const url of urls)if(!known.has(url)){
      const error=(parsed.data.errors??[]).find(x=>x.url===url);
      output.push({url,status:"unavailable",reason:error?.code??error?.error??(parsed.isError?"tool_error":"missing_fetch_result"),response_sha256:parsed.sha,semantic_review_complete:false});
    }
    return output;
  }
  let cursor=0,completed=0,success=0,unresolved=0,lastSearch=0,fatal=null;
  let authConfirmed=!!cfg.manifest?.connected_authorization;
  const done=new Set(cfg.completed_ids??[]);
  completed=done.size;
  async function search(task,query){
    const due=Math.max(Date.now(),lastSearch+2500);lastSearch=due;
    if(due>Date.now())await new Promise(resolve=>setTimeout(resolve,due-Date.now()));
    if(fatal)throw new Error(fatal);
    const request={query,domain_type:"news",language:"en",location:"US"};
    const result=await tools.mcp__codex_apps__tinyfish_search(request);
    const parsed=parseResult(result);
    if(!parsed.isError)authConfirmed=true;
    return {query,request,source:"TinyFish connected search plugin",response_sha256:parsed.sha,
      searched_at:new Date().toISOString(),result:{status:parsed.isError?"unavailable":"ok",sources:(parsed.data.results??[]).filter(x=>publicUrl(x.url)).map(x=>({url:x.url,title:x.title,published_at:x.date,publisher:x.publisher,position:x.position,snippet:x.snippet??""}))}};
  }
  async function fetch(urls){
    const result=await tools.mcp__codex_apps__tinyfish_fetch_content({urls,format:"markdown",links:false,image_links:false,iframe_links:false,page_metadata:true,per_url_timeout_ms:30000,ttl:0});
    const parsed=parseResult(result);
    if(!parsed.isError)authConfirmed=true;
    return documentsFrom(parsed,urls);
  }
  async function one(task,index){
    const record={task_id:task.task_id,title:task.title,ticker:task.ticker,published_at:task.published_at,
      provider:"tinyfish_connected_plugin",searches:[],documents:[],llm_called:false,sentiment_assessed:false};
    const seen=new Set();
    for(let attempt=0;attempt<2;attempt++){
      const query=attempt===0?task.title:task.ticker+" "+task.title+" "+(task.published_at??"").slice(0,10);
      let searched;
      if(index===0&&attempt===0&&load("tinyfish_probe_search")){
        const parsed=parseResult(load("tinyfish_probe_search"));
        if(!parsed.isError)authConfirmed=true;
        searched={query,request:{query,domain_type:"news",language:"en",location:"US"},source:"TinyFish connected search plugin",response_sha256:parsed.sha,searched_at:new Date().toISOString(),result:{status:"ok",sources:(parsed.data.results??[]).filter(x=>publicUrl(x.url)).map(x=>({url:x.url,title:x.title,published_at:x.date,publisher:x.publisher,position:x.position,snippet:x.snippet??""}))}};
      }else searched=await search(task,query);
      record.searches.push(searched);
      const sources=[];
      for(const item of searched.result.sources)if(!seen.has(item.url)){
        seen.add(item.url);sources.push(item);if(sources.length===2)break;
      }
      const urls=sources.map(x=>x.url);
      let docs=[];
      if(urls.length){
        if(index===0&&attempt===0&&load("tinyfish_probe_fetch"))docs=documentsFrom(parseResult(load("tinyfish_probe_fetch")),urls);
        else docs=await fetch(urls);
        record.documents.push(...docs);
      }
      if(record.documents.some(x=>x.status==="fetched"))break;
    }
    record.status=record.documents.some(x=>x.status==="fetched")?"fetched_for_review":"fetch_unresolved";
    await persist(cfg.folder+"/items/"+task.task_id+".json",record);
    done.add(task.task_id);completed++;
    if(record.status==="fetched_for_review")success++;else unresolved++;
    store("tinyfish_native_progress",{completed,total:tasks.length,fetched_this_run:success,unresolved_this_run:unresolved});
    if(completed%10===0 || completed===tasks.length)notify({completed,total:tasks.length,fetched_this_run:success,unresolved_this_run:unresolved});
  }
  async function worker(){
    while(cursor<tasks.length&&!fatal){
      const index=cursor++,task=tasks[index];
      if(done.has(task.task_id))continue;
      try{await one(task,index);}catch(error){
        if(String(error.message).includes("FATAL_")){fatal=error.message;break;}
        await persist(cfg.folder+"/items/"+task.task_id+".json",{task_id:task.task_id,title:task.title,ticker:task.ticker,published_at:task.published_at,provider:"tinyfish_connected_plugin",status:"fetch_unresolved",searches:[],documents:[],error:String(error.message),llm_called:false,sentiment_assessed:false});
        completed++;unresolved++;
        store("tinyfish_native_progress",{completed,total:tasks.length,fetched_this_run:success,unresolved_this_run:unresolved});
      }
    }
  }
  const outcomes=await Promise.allSettled(Array.from({length:8},()=>worker()));
  const failure=outcomes.find(x=>x.status==="rejected");
  const status={completed,total:tasks.length,all_collected:completed===tasks.length,fetched_this_run:success,unresolved_this_run:unresolved,fatal_error:fatal??(failure?String(failure.reason):null),llm_called:false,cleanup_performed:false};
  await persist(cfg.folder+"/manifest.json",{...cfg.manifest,workflow_id:cfg.workflow_id??cfg.folder.split("/").at(-3),batch_id:cfg.batch_id??cfg.folder.split("/").at(-1),provider:"tinyfish_connected_plugin",source_title_count:tasks.length,connected_authorization:authConfirmed,orchestration:"one deterministic Search/Fetch program",cleanup_enabled:false});
  await persist(cfg.folder+"/driver-result.json",status);
  text(status);
}
await runTinyFishBatch();
```

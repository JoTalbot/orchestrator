# Карта API arena.ai (реверс-инжиниринг)

Собрано автоматически `arena_agent/scan_api.py` 2026-09-17 08:54 из 88 JS-бандлов.

Колонка «статус» — результат зондирования GET-запросом из вкладки браузера (`scan_api.py probe`): `200` работает, `400/422` маршрут есть, но нужны параметры, `401/403` нужен контекст/права, `404` — нет такого маршрута (или нужен другой метод).

| Метод | Путь | Источник | Статус | Заметка |
|---|---|---|---|---|
| GET | `/agent/{id}` | manual,page | 200 — RSC-пейлоад с транскриптом и publicAccessToken | HTML страницы чата: внутри RSC-пейлоад (self.__next_f.push) с транскриптом, session.publicAccessToken и курсором пагинации. |
| GET | `/ai-proxy/` | literal | 200 (тело не публикуем: личные данные) |  |
| GET | `/ai-proxy/realtime/v1/batches/{batchId}` | manual |  | Состояние батча запусков. |
| GET | `/ai-proxy/realtime/v1/runs` | manual |  | Список запусков Trigger.dev (внутренняя отладка). |
| GET | `/ai-proxy/realtime/v1/runs/{runId}` | manual |  | Состояние конкретного запуска. |
| GET | `/ai-proxy/realtime/v1/sessions/{id}/channels/{channel}` | manual |  | Поток отдельного канала сессии (вариант /out). |
| POST | `/ai-proxy/realtime/v1/sessions/{id}/in/append` | manual,fetch | 200 — проверено записью (arena_api.send_message) | РАБОТАЕТ: «вход» агента. Заголовки Authorization: Bearer <publicAccessToken>, x-trigger-source: sdk, x-part-id: uuid. Тело {kind:'message', payload:{message, chatId, trigger:'submit-message', messageId, metadata}} или {kind:'stop'}. |
| GET | `/ai-proxy/realtime/v1/sessions/{id}/out` | manual,fetch | 200 SSE — проверено чтением потока (arena_api.stream_out) | РАБОТАЕТ: SSE-поток ответа агента, Authorization: Bearer <publicAccessToken>. Поддерживает Last-Event-ID (без него — реплей всей истории записей) и X-Peek-Settled. События приходят пачками {records:[{seq_num,timestamp,body:'{"data":{"type":"text-delta"|"reasoning-delta"|"tool-input-delta"|"start"...}}'}]}. |
| GET | `/ai-proxy/realtime/v1/streams/{runId}/webdev-stream` | manual |  |  |
| GET | `/ai-proxy/realtime/v1/streams/{runId}/{streamId}` | manual |  | Поток запуска. |
| POST | `/ai-proxy/realtime/v1/streams/{runId}/{streamId}/append` | manual |  | Дозапись в поток запуска. |
| GET | `/api/billing/balance` | rpc | 200 (тело не публикуем: личные данные) | РАБОТАЕТ: {creditsRemaining, dailyFreeCredits, refreshedAt} — ежедневная квота 1 000 000 кредитов. |
| GET | `/api/chat` | literal | 403 {"error":"Route not allowed"} |  |
| GET | `/api/chat/agent-models` | rpc | 403 {"error":"Not allowed"} | 403 «Not allowed» даже из вкладки — вероятно нужен контекст страницы агента/права. |
| POST | `/api/chat/trigger-session` | rpc | 403 {"error":"Route not allowed"} | Старт/пересоздание Trigger.dev-сессии: {"sessionId", "timezone"}. GET отдаёт 403 — только POST. |
| POST | `/api/chat/trigger-token` | rpc | 403 {"error":"Route not allowed"} | РАБОТАЕТ (17.09.2026): тело {"sessionId": "<chat_id>"} → {"token": "<publicAccessToken JWT>"}. Дешёвая замена чтению RSC-пейлоада. |
| GET | `/api/chat/workspace/cas/user/{param}` | literal | 400 {"success":false,"error":{"issues":[{"code":"too_small","minimum":43,"type":"string","incl | Отдача загруженного пользователем файла по CAS-хешу (43 символа). |
| DELETE | `/api/chat/{id}` | rpc | 403 {"error":"Route not allowed"} |  |
| POST | `/api/chat/{id}/archive` | rpc | skip (небезопасно зондировать) |  |
| POST | `/api/chat/{id}/arena-feedback` | rpc | 403 {"error":"Route not allowed"} |  |
| GET | `/api/chat/{id}/cost` | rpc | 403 {"error":"Unauthorized"} | 403 Unauthorized — стоимость недоступна этому аккаунту/маршруту. |
| GET | `/api/chat/{id}/messages` | rpc | 400 {"success":false,"error":{"issues":[{"code":"invalid_type","expected":"string","received": | РАБОТАЕТ: query cursor,limit(≤50) → {messages[], pagination{cursor,hasMore}} — пагинация НАЗАД по истории. |
| GET | `/api/chat/{id}/preview` | rpc | 200 (тело не публикуем: личные данные) | РАБОТАЕТ: состояние превью-песочницы {registered, sandboxState, processes[], ports[]}. |
| POST | `/api/chat/{id}/preview/heartbeat` | rpc | 403 {"error":"Route not allowed"} |  |
| POST | `/api/chat/{id}/review-feedback` | rpc | 403 {"error":"Route not allowed"} |  |
| POST | `/api/chat/{id}/unarchive` | rpc | skip (небезопасно зондировать) |  |
| GET | `/api/chat/{id}/workspace/latest` | rpc | 200 (тело не публикуем: личные данные) | РАБОТАЕТ: ?includeManifest=true → {manifestNodeId, leafMessageId, ...} — снимок песочницы агента. |
| POST | `/api/chat/{id}/workspace/preview-token` | rpc | 400 {"success":false,"error":{"issues":[{"validation":"uuid","code":"invalid_string","message" |  |
| GET | `/api/chat/{id}/workspace/{manifestNodeId}` | rpc | 404 {"error":"Node \"00000000-0000-0000-0000-000000000000\" not found in session \"01a06bdc-ef | Содержимое файла/каталога песочницы по nodeId; каталог — суффикс /dir?path=. |
| GET | `/api/chat/{id}/workspace/{manifestNodeId}/dir` | rpc | 404 {"error":"Node \"00000000-0000-0000-0000-000000000000\" not found in session \"01a06bdc-ef |  |
| GET | `/api/chat/{id}/workspace/{nodeId}/dir` | manual | 200 — листинг каталога песочницы (?path=) |  |
| GET | `/api/chat/{param}/workspace/{param}/download` | literal | 404 {"error":"Session \"00000000-0000-0000-0000-000000000000\" not found"} |  |
| GET | `/api/coding-agent/sessions` | literal | 403 {"error":"Route not allowed"} |  |
| POST | `/api/coding-agent/sessions` | fetch | 403 {"error":"Route not allowed"} |  |
| GET | `/api/coding-agent/sessions/{param}` | fetch,literal | 404 {"error":"not_found"} |  |
| GET | `/api/coding-agent/sessions/{param}/diff/download` | literal | 404 {"error":"Coding session \"00000000-0000-0000-0000-000000000000\" not found"} |  |
| GET | `/api/coding-agent/sessions/{param}/diff/{param}` | fetch,literal | 404 {"error":"Coding session \"00000000-0000-0000-0000-000000000000\" not found"} |  |
| GET | `/api/coding-agent/sessions/{param}/diff/{param}/file` | fetch,literal | 400 {"success":false,"error":{"issues":[{"code":"invalid_type","expected":"string","received": |  |
| GET | `/api/coding-agent/sessions/{param}/workflow-checks` | fetch,literal | 404 {"error":"not_found"} |  |
| GET | `/api/coding/github/branches` | literal | 400 {"success":false,"error":{"issues":[{"code":"invalid_type","expected":"number","received": |  |
| GET | `/api/coding/github/connect/install` | literal | ERR JS-ошибка: {"exceptionId": 1, "text": "Uncaught (in promise) TypeError: Failed t |  |
| GET | `/api/coding/github/connect/start` | literal | ERR JS-ошибка: {"exceptionId": 2, "text": "Uncaught (in promise) TypeError: Failed t |  |
| GET | `/api/coding/github/connection` | literal | 200 (тело не публикуем: личные данные) | РАБОТАЕТ: {status:'connected'} — GitHub подключён к аккаунту. |
| GET | `/api/coding/github/disconnect` | literal | skip (небезопасно зондировать) |  |
| POST | `/api/coding/github/disconnect` | fetch | skip (небезопасно зондировать) |  |
| GET | `/api/coding/github/repos` | literal | 200 (тело не публикуем: личные данные) | РАБОТАЕТ: список репозиториев GitHub, подключённых к аккаунту. |
| GET | `/api/coding/github/status` | literal | 200 (тело не публикуем: личные данные) |  |
| GET | `/api/connectors` | literal | 502 {"type":"https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloud | 502 — апстрим Cloudflare сломан (маршрут есть, но не работает). |
| GET | `/api/connectors/status` | literal | 502 {"type":"https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloud |  |
| GET | `/api/connectors/sync` | literal | 403 {"error":"Route not allowed"} |  |
| GET | `/api/connectors/tools` | literal | 502 {"type":"https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloud |  |
| GET | `/api/connectors/{param}` | literal | 403 {"error":"Route not allowed"} |  |
| GET | `/api/connectors/{param}/link-token` | literal | 403 {"error":"Route not allowed"} |  |
| GET | `/api/connectors/{param}/tools` | literal | 502 {"type":"https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloud |  |
| GET | `/api/early_access_features/` | literal | 403 {"error":"Route not allowed"} |  |
| GET | `/api/evaluation/webdev/{messageId}` | rpc | 200 (тело не публикуем: личные данные) |  |
| GET | `/api/evaluation/webdev/{messageId}/database` | rpc | 404 {"error":"Webdev manifest not ready for message 00000000-0000-0000-0000-000000000000"} |  |
| GET | `/api/evaluation/webdev/{param}/stream-credentials` | fetch,literal | 404 {"error":"Message not found"} |  |
| GET | `/api/evaluation/{id}/cost` | rpc | 403 {"error":"Unauthorized"} |  |
| GET | `/api/history/search` | rpc | 400 {"success":false,"error":{"issues":[{"code":"invalid_type","expected":"string","received": | РАБОТАЕТ: поиск по чатам, query q/limit/includeArchived. |
| GET | `/api/history/unified` | rpc | 200 (тело не публикуем: личные данные) | РАБОТАЕТ: список чатов, query cursor/limit(≤50)/includeArchived/type=agentic → {entries[], nextCursor, total}. |
| PATCH | `/api/history/{type}/{id}` | rpc | skip (неизвестный параметр) |  |
| GET | `/api/me` | fetch,literal | 200 (тело не публикуем: личные данные) | РАБОТАЕТ: профиль (email, username, subscription, tier). |
| GET | `/api/me/pulse` | rpc | 200 (тело не публикуем: личные данные) | РАБОТАЕТ: {pulse} — признак активности/квоты. |
| POST | `/api/me/update-tou-consent` | rpc | 403 {"error":"Route not allowed"} |  |
| GET | `/api/product_tours/` | literal | 403 {"error":"Route not allowed"} |  |
| POST | `/api/storage/generate-agent-upload-url` | rpc | 403 {"error":"Route not allowed"} | РАБОТАЕТ: {"hash": base64url(sha256(байты)) без '=' (43 символа), "contentType", "size"} → {uploadUrl, key}; затем PUT uploadUrl с байтами; url файла в сообщении = /api/chat/workspace/cas/user/{hash}. |
| GET | `/api/surveys/` | literal | 403 {"error":"Route not allowed"} |  |
| GET | `/api/web_experiments/` | literal | 403 {"error":"Route not allowed"} |  |
| GET | `/nextjs-api/auto-modality` | literal | 405  |  |
| POST | `/nextjs-api/auto-modality` | fetch | 405  |  |
| GET | `/nextjs-api/autoeval/release-banner` | fetch,literal | 200 (тело не публикуем: личные данные) |  |
| GET | `/nextjs-api/factuality/ratings` | fetch,literal | 200 (тело не публикуем: личные данные) |  |
| GET | `/nextjs-api/resend-verification` | literal | 405  |  |
| POST | `/nextjs-api/resend-verification` | fetch | 405  |  |
| GET | `/nextjs-api/reset-password/change` | literal | 405  |  |
| POST | `/nextjs-api/reset-password/change` | fetch | 405  |  |
| GET | `/nextjs-api/reset-password/confirm` | literal | 405  |  |
| POST | `/nextjs-api/reset-password/confirm` | fetch | 405  |  |
| GET | `/nextjs-api/reset-password/request` | literal | 405  |  |
| POST | `/nextjs-api/reset-password/request` | fetch | 405  |  |
| GET | `/nextjs-api/sign-in/email` | literal | 405  |  |
| POST | `/nextjs-api/sign-in/email` | fetch | 405  |  |
| GET | `/nextjs-api/sign-in/google` | literal | 200 (тело не публикуем: личные данные) |  |
| GET | `/nextjs-api/sign-out` | literal | skip (небезопасно зондировать) |  |
| GET | `/nextjs-api/sign-up` | literal | 405  |  |
| POST | `/nextjs-api/sign-up` | fetch | 405  |  |
| GET | `/nextjs-api/sign-up/magic-link` | literal | 405  |  |
| POST | `/nextjs-api/sign-up/magic-link` | fetch | 405  |  |
| GET | `/nextjs-api/stream/create-chat` | literal | skip (небезопасно зондировать) |  |
| POST | `/nextjs-api/stream/create-chat` | fetch | skip (небезопасно зондировать) | РАБОТАЕТ: создание чата Agent Mode. Тело {message:{id:uuid7,role:'user',parts:[{type:'text',text}|{type:'file',url,mediaType,filename}]}, recaptchaV3Token, timezone, modelId?, harnessId?, enabledConnectors?} → {id}. |
| GET | `/nextjs-api/stream/create-evaluation` | literal | skip (небезопасно зондировать) |  |
| POST | `/nextjs-api/stream/create-evaluation` | fetch | skip (небезопасно зондировать) |  |
| GET | `/nextjs-api/stream/post-to-evaluation/{param}` | literal | 405  |  |
| POST | `/nextjs-api/stream/post-to-evaluation/{param}` | fetch | 405  |  |
| GET | `/nextjs-api/stream/rerun/{param}` | literal | skip (небезопасно зондировать) |  |
| POST | `/nextjs-api/stream/rerun/{param}` | fetch | skip (небезопасно зондировать) |  |
| GET | `/nextjs-api/stream/resample/{param}` | literal | skip (небезопасно зондировать) |  |
| POST | `/nextjs-api/stream/resample/{param}` | fetch | skip (небезопасно зондировать) |  |
| GET | `/nextjs-api/stream/resume-video-workflow/{param}` | fetch,literal | 404 {"error":"Session not found"} |  |
| GET | `/nextjs-api/stream/resume-webdev/{param}` | literal | 404 {"error":"Message not found"} |  |
| GET | `/nextjs-api/stream/retry-evaluation-session-message/{param}/messages/{param}` | literal | skip (небезопасно зондировать) |  |
| PUT | `/nextjs-api/stream/retry-evaluation-session-message/{param}/messages/{param}` | fetch | skip (небезопасно зондировать) |  |
| GET | `/nextjs-api/stream/skip-direct-battle/{param}` | literal | 405  |  |
| POST | `/nextjs-api/stream/skip-direct-battle/{param}` | fetch | 405  |  |
| GET | `/nextjs-api/stream/stop/{param}/messages/{param}` | literal | skip (небезопасно зондировать) |  |
| POST | `/nextjs-api/stream/stop/{param}/messages/{param}` | fetch | skip (небезопасно зондировать) |  |
| GET | `https://trigger.dev/docs/queue-concurrency#deadlock` | literal | skip (внешний хост) |  |
| GET | `https://trigger.dev/docs/queue-concurrency#waiting-for-a-subtask-on-the-same-queue` | literal | skip (внешний хост) |  |
| GET | `https://trigger.dev/docs/troubleshooting#parallel-waits-are-not-supported` | literal | skip (внешний хост) |  |
| GET | `https://trigger.dev/docs/troubleshooting#task-run-stalled-executing` | literal | skip (внешний хост) |  |
| GET | `https://trigger.dev/docs/troubleshooting#uncaught-exceptions` | literal | skip (внешний хост) |  |
| GET | `https://trigger.dev/docs/upgrading-beta` | literal | skip (внешний хост) |  |
| GET | `https://trigger.dev/docs/v3/machines` | literal | skip (внешний хост) |  |
| GET | `{param}/api/v1/reports/{param}` | fetch | 200 (тело не публикуем: личные данные) |  |
| POST | `{param}/api/v3/batches/{param}/items` | fetch | 200 (тело не публикуем: личные данные) |  |

## Контекст вызовов (как их дёргает веб-клиент)

### GET /api/billing/balance
```js
();if(!e.ok){let t=await e.text();throw Error(`Failed to fetch billing balance (${e.status}): ${t}`)}return c.vw.parse(await e.json())}var g=n(88148);function x(e){let t,n,c=(0,r.c)(14),{settledItemCount:d,isFreeSession:u}=e,m=(0,i.y)(),p=(0,g.kH)(g.Q_),x=m&&!p;c[0]!==d||c[1]!==x
```

### GET /api/chat/agent-models
```js
();if(!e.ok){let t=await e.text();throw Error(`Failed to fetch agent models (${e.status}): ${t}`)}return z.parse(await e.json()).models}let T=(0,a(52051).A)("FlaskConical",[["path",{d:"M14 2v6a2 2 0 0 0 .245.96l5.51 10.08A2 2 0 0 1 18 22H6a2 2 0 0 1-1.755-2.96l5.51-10.08A2 2 0 0 
```

### POST /api/chat/trigger-session
```js
({json:{sessionId:e,timezone:e1.timezone}});if(!t.ok)throw Error("Failed to start chat session");return t.json()},[e1.timezone,e]),e4=(0,l.useMemo)(()=>(function(e=globalThis.fetch){return async(t,s,a)=>{try{let n=await e(t,s);return n.ok||U({endpoint:a.endpoint,url:t,statusCode:
```

### POST /api/chat/trigger-token
```js
({json:{sessionId:e}});if(!t.ok)throw Error("Failed to create chat access token");let{token:s}=await t.json();return s},[e]),e3=(0,l.useCallback)(async()=>{let t=await eu.F.chat["trigger-session"].$post({json:{sessionId:e,timezone:e1.timezone}});if(!t.ok)throw Error("Failed to st
```

### DELETE /api/chat/{id}
```js
({param:{id:e}})).ok)throw Error("Failed to delete agentic session")}async function f(e){if(!(await p.F.chat[":id"].archive.$post({param:{id:e}})).ok)throw Error("Failed to archive agentic session")}async function h(e){if(!(await p.F.chat[":id"].unarchive.$post({param:{id:e}})).o
```

### POST /api/chat/{id}/archive
```js
({param:{id:e}})).ok)throw Error("Failed to archive agentic session")}async function h(e){if(!(await p.F.chat[":id"].unarchive.$post({param:{id:e}})).ok)throw Error("Failed to unarchive agentic session")}let S=()=>{let e,a,t,s,o,l=(0,i.c)(12),d=(0,n.jE)(),u=(0,g.kH)(N);return l[0
```

### POST /api/chat/{id}/arena-feedback
```js
({param:{id:e.sessionId},json:{sessionNodeId:e.sessionNodeId,text:e.text,recaptchaV3Token:e.recaptchaV3Token}});if(!t.ok)return{status:"error",message:await au(t),source:t};return{status:"success"}}catch(e){return{status:"error",message:e instanceof Error&&e.message?e.message:nul
```

### GET /api/chat/{id}/cost
```js
({param:{id:e},query:{includeSession:"false",messageIds:t.join(",")}});if(!a.ok){let e=await a.text();throw Error(`Failed to fetch agent message costs (${a.status}): ${e}`)}return m.parse(await a.json()).messages}async function v(e,s){if("agent"===e){let e=await b.F.chat[":id"].c
```

### GET /api/chat/{id}/messages
```js
({param:{id:e},query:{cursor:t,limit:a}},{init:{signal:s}});if(!n.ok)throw new eb(n.status);let r=await n.json();return{messages:r.messages,pagination:r.pagination}}var ev=s(53664),ej=s(77520),e_=s(18761);let ew=e=>[e_.l,"latest-workspace",e];function eN(e,t){e.invalidateQueries(
```

### GET /api/chat/{id}/preview
```js
({param:{id:e}});if(!s.ok)throw Error(`Failed to fetch sandbox preview (${s.status})`);return await s.json()}}));function eM(e){return e?`${ek.lX}/${e.repoName}`:ek.lX}var eF=s(6244);function eL(e){let t=new Set;for(let s of e)if("assistant"===s.role)for(let e of s.parts)"tool-pr
```

### POST /api/chat/{id}/preview/heartbeat
```js
({param:{id:e}}):await eu.F.chat[":id"].preview.$get({param:{id:e}});if(!s.ok)throw Error(`Failed to fetch sandbox preview (${s.status})`);return await s.json()}}));function eM(e){return e?`${ek.lX}/${e.repoName}`:ek.lX}var eF=s(6244);function eL(e){let t=new Set;for(let s of e)i
```

### POST /api/chat/{id}/review-feedback
```js
({param:{id:e.sessionId},json:"check_in"===e.feedback.type?{...t,action:e.feedback.value}:{...t,feedback:e.feedback}});if(!s.ok)return{status:"error",error:await tp(s)};return{status:"success"}}catch(e){return{status:"error",error:e instanceof Error?e:Error(tu)}}}var tf=s(82708),
```

### POST /api/chat/{id}/unarchive
```js
({param:{id:e}})).ok)throw Error("Failed to unarchive agentic session")}let S=()=>{let e,a,t,s,o,l=(0,i.c)(12),d=(0,n.jE)(),u=(0,g.kH)(N);return l[0]!==u?(e=async e=>{let{entry:a,title:t}=e;if(!u)throw new E;let i=await p.F.history[":type"][":id"].$patch({param:{type:a.type,id:a.
```

### GET /api/chat/{id}/workspace/latest
```js
({param:{id:m},query:{includeManifest:"true"}});if(!e.ok)throw Error(`Failed to fetch latest workspace (${e.status})`);let t=await e.json();return{manifestNodeId:t.manifestNodeId,leafMessageId:t.leafMessageId,manifest:t.manifest?ej.If.parse(t.manifest):null}}})),p[0]=m,p[1]=t;els
```

### POST /api/chat/{id}/workspace/preview-token
```js
({param:{id:e},json:{manifestNodeId:t}});if(501===s.status)return null;if(!s.ok)throw Error(`Failed to mint workspace preview grant (${s.status})`);return lL.parse(await s.json())}})),l[0]=t,l[1]=e,l[2]=s;else s=l[2];let c=o&&null!=e&&null!=t;l[3]!==s||l[4]!==c?(a={...s,enabled:c
```

### GET /api/chat/{id}/workspace/{manifestNodeId}
```js
({param:{id:e,manifestNodeId:t}});if(!s.ok)throw Error(`Failed to fetch workspace manifest (${s.status})`);let n=await s.json();return n.manifest?a.If.parse(n.manifest):null}})),d=(e,t,s)=>(0,i.IY)((0,n.j)({queryKey:[l.l,"workspace-dir",e,t,s??""],staleTime:1/0,enabled:null!=e&&n
```

### GET /api/chat/{id}/workspace/{manifestNodeId}/dir
```js
({param:{id:e,manifestNodeId:t},query:{path:s}});if(!n.ok)throw Error(`Failed to fetch workspace directory (${n.status})`);let i=await n.json();return i.directory?a.jc.parse(i.directory):null}}))},17050:(e,t,s)=>{"use strict";s.d(t,{p:()=>r});var a=s(6740),n=s(30265);let r=s(4728
```

### GET /api/evaluation/webdev/{messageId}
```js
({param:{messageId:e}});if(!t.ok){let e=await t.text();throw Error(`Failed to fetch webdev metadata (${t.status}): ${e}`)}let a=n.Un.parse(await t.json());return"pending"===a.status?{status:"pending",files:[]}:function(e,t){let a=e.binaryFiles??[],n=e.template.kind;switch(e.build
```

### GET /api/evaluation/webdev/{messageId}/database
```js
({param:{messageId:e},query:a});if(!i.ok){let e=await i.text();throw Error(`Failed to fetch webdev database (${i.status}): ${e}`)}return n.Y7.parse(await i.json())}},15324:(e,t,a)=>{"use strict";a.d(t,{Hw:()=>c,Iv:()=>u,JO:()=>l,Wy:()=>o,Z1:()=>s});var n=a(52284),i=a(68039),r=a(7
```

### GET /api/evaluation/{id}/cost
```js
({param:{id:s}});if(!t.ok){let e=await t.text();throw Error(`Failed to fetch evaluation session cost (${t.status}): ${e}`)}return g.parse(await t.json())}var y=t(47288);let M={messages:{},missingIds:[]};function C(e=M,s,t=[]){let a={...e.messages,...s};return{messages:a,missingId
```

### GET /api/history/search
```js
({query:{q:e,includeArchived:a?"true":"false",cursor:t??void 0,limit:i,mode:n,modality:r,type:s,archivedOnly:void 0!==o?o?"true":"false":void 0}});if(!l.ok){let e=await l.text();throw Error(`Failed to search history (${l.status}): ${e||"Unknown error"}`)}return y.parse(await l.js
```

### GET /api/history/unified
```js
({query:{cursor:e??void 0,limit:a,includeArchived:t?"true":"false",mode:i,modality:n,type:r,archivedOnly:void 0!==s?s?"true":"false":void 0}});if(!o.ok){let e=await o.text();throw Error(`Failed to fetch unified history (${o.status}): ${e||"Unknown error"}`)}return E.parse(await o
```

### PATCH /api/history/{type}/{id}
```js
({param:{type:a.type,id:a.id},json:{title:t.trim()}});if(!i.ok)throw new E(i);return i.json()},l[0]=u,l[1]=e):e=l[1],l[2]!==d?(a=async e=>{let{entry:a,title:t}=e,i=t.trim();c.analytics.capture({action:"chat_rename",params:I(a)}),await b(d,y);let n=d.getQueriesData({queryKey:z.l6.
```

### GET /api/me/pulse
```js
();if(!e.ok){let t=await e.text();throw Error(`Failed to fetch user pulse (${e.status}): ${t}`)}return l.Yw.parse(await e.json())}var p=n(47288),g=n(52284),x=n(88148);function h(e){let t,n,l,c,d=(0,r.c)(12),{settledItemCount:u,isFreeSession:f,surface:h}=e,b=(0,x.kH)(x.Q_),y=!b;d[
```

### POST /api/me/update-tou-consent
```js
().catch(()=>{console.error("Failed to persist TOU consent to server")})}catch{d.analytics.capture({action:"terms_of_service_modal_accept_failed"}),(0,m.K)("Failed to accept terms-of-use"),n?.reject(),a(null)}finally{b(!1)}};return(0,r.jsx)(c.lG,{open:e,onOpenChange:e=>{e||(b(!1)
```

### POST /api/storage/generate-agent-upload-url
```js
({json:{hash:t,contentType:a,size:e.size}});if(!n.ok){let e=Error(`Failed to generate agent upload URL: ${n.status}`);if(n.status>=400&&n.status<500)throw new c.l(e);throw e}return n.json()});return await m(async()=>{let t=await fetch(s,{method:"PUT",body:e,headers:{"Content-Type
```

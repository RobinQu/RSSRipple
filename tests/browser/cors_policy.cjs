// Synthetic localhost fixture only. Start cors_fixture_server.py first.
const { chromium } = require('playwright');
const http = require('node:http');
const {execFileSync} = require('node:child_process');
const fs=require('node:fs');
(async()=>{
 const servers=[];let browser;
 const result={synthetic:true, browser:'Chromium', cases:[]};
 try {
  for(const port of [32901,32902]){const s=http.createServer((req,res)=>{res.setHeader('Content-Type','text/html');res.end('<!doctype html><title>Synthetic source</title>');});await new Promise(r=>s.listen(port,'127.0.0.1',r));servers.push(s);}
  browser=await chromium.launch({headless:true,...(process.env.BROWSER_EXECUTABLE ? {executablePath:process.env.BROWSER_EXECUTABLE} : {}),args:['--no-proxy-server']});
  const ctx=await browser.newContext();const page=await ctx.newPage();const api='http://127.0.0.1:32900';
  await page.goto(api+'/browser-probe');
  const code=execFileSync(process.env.TEST_PYTHON || 'python3',['-c','import pyotp;print(pyotp.TOTP("JBSWY3DPEHPK3PXP").now())']).toString().trim();
  const login=await page.evaluate(async({api,code})=>{const r=await fetch(api+'/api/v1/auth/otp',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code})});return {status:r.status,body:await r.json()};},{api,code});
  if(login.status!==200)throw Error(JSON.stringify(login));
  result.cookieAttributes=(await ctx.cookies()).map(({name,httpOnly,sameSite,secure})=>({name,httpOnly,sameSite,secure}));
  for(const [name,origin] of [['allowed-same-site','http://127.0.0.1:32901'],['denied-same-site','http://127.0.0.1:32902'],['denied-cross-site','http://localhost:32902']]){
   await page.goto(origin);
   const observed=[];const listener=req=>{if(req.url().startsWith(api))observed.push(req);};page.on('request',listener);
   const read=await page.evaluate(async api=>{try{const r=await fetch(api+'/api/v1/auth/status',{credentials:'include'});return {status:r.status,body:await r.json()};}catch(e){return {error:e.name};}},api);
   page.off('request',listener);
   const headers=await Promise.all(observed.map(async r=>{const h=await r.allHeaders();return {method:r.method(),cookieSent:!!h.cookie,origin:h.origin};}));
   result.cases.push({name,read,requests:headers});
  }
  await page.goto('http://127.0.0.1:32902');
  const logout=await page.evaluate(async api=>{try{const r=await fetch(api+'/api/v1/auth/logout',{method:'POST',credentials:'include'});return {status:r.status};}catch(e){return {error:e.name};}},api);
  await page.goto(api+'/browser-probe');
  const after=await page.evaluate(async()=> (await (await fetch('/api/v1/auth/status')).json()).data.authenticated);
  result.logout={browserRead:logout,authenticatedAfter:after};
  if(!after)throw Error('Untrusted logout still clears cookie');
  await page.goto('http://127.0.0.1:32901');
  const allowedLogout=await page.evaluate(async api=>(await fetch(api+'/api/v1/auth/logout',{method:'POST',credentials:'include'})).status,api);
  await page.goto(api+'/browser-probe');
  const finalAuth=await page.evaluate(async()=> (await (await fetch('/api/v1/auth/status')).json()).data.authenticated);
  result.allowedLogout={status:allowedLogout,authenticatedAfter:finalAuth};
  if(allowedLogout!==200||finalAuth)throw Error('Allowed logout broken');
  if(result.cases[0].read.body.data.authenticated!==true)throw Error('Allowed read broken');
  if(result.cases.slice(1).some(c=>c.read.error!=='TypeError'))throw Error('Unlisted read exposed');
  fs.writeFileSync(process.env.BROWSER_REPORT || '/tmp/rssripple-cors-browser.json',JSON.stringify(result,null,2));
  console.log(JSON.stringify(result,null,2));
 }finally{if(browser)await browser.close();for(const s of servers)await new Promise(r=>s.close(r));}
})().catch(e=>{console.error(e);process.exitCode=1});
